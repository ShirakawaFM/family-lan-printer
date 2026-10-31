# -*- coding: utf-8 -*-
"""
家庭内网打印工具（Windows 单文件版）

用途：
    在 Windows 电脑上运行本文件，手机连同一个 WiFi 后扫码打开网页，
    输入文字、选择图片，或从手机/微信聊天记录里选择文件，一键用默认打印机打印。

运行：
    python print_server.py

技术栈：
    Python 3 + Flask + pywin32 + Pillow + qrcode
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import html as html_lib
import importlib
import importlib.util
import io
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache, wraps
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Optional

# =============================================================================
# 一、用户配置区（只改这里，其他代码不用动）
# =============================================================================

# 监听地址和端口。0.0.0.0 表示同一 WiFi 下的手机都可以访问。
HOST = "0.0.0.0"
PORT = 5000

# 【访问密码】在这里修改。建议改成家里好记、但外人猜不到的密码。
# 这不是用户注册/账号系统，只是全家共用一个使用密码。
ACCESS_PASSWORD = "123456"

# 外网穿透教程开关。True/False 只影响控制台是否提示“代码末尾有教程”，
# 网页上永远只展示内网二维码，不会展示外网二维码。
ENABLE_FRP_GUIDE = True

# 是否在程序目录生成 qr_wall.png（贴墙用二维码图片）。
EXPORT_QR_IMAGE = True
QR_WALL_FILENAME = "qr_wall.png"

# 如果电脑有多张网卡（例如 VPN），自动识别可能选错 IP。
# 一般留空；确实需要时，在这里手工填写电脑的局域网 IPv4，例如 "192.168.1.23"。
LAN_IP_OVERRIDE = ""

# 临时文件目录名（自动创建在程序所在目录）。
TEMP_DIR_NAME = "temp_print"

# 文字打印：A4、宋体、24 号、固定 20 毫米页边距、左对齐。
# 请把打印机纸盒/纸张大小设置为 A4（程序按 A4 版面排版）。
PAGE_MARGIN_MM = 20.0
TEXT_FONT_SIZE_PT = 24
TEXT_LINE_SPACING = 1.5
RENDER_DPI = 300

# 图片上传上限：8 MB（只接受 jpg / png / webp）。
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# 防止一张 8MB 的超大尺寸图片在解码时吃光内存（5000 万像素足够手机照片）。
MAX_IMAGE_PIXELS = 50_000_000

# 文字长度上限，防止一次误粘贴把打印机打空。20000 字约几十页。
MAX_TEXT_CHARS = 20000

# 手机文件 / 聊天记录文件上传。
# 支持 PDF、Word、Excel、PPT、TXT、HTML、图片；Office 文件会调用本机 Office 转 PDF。
FILE_MAX_BYTES = 50 * 1024 * 1024
FILE_PREVIEW_PAGES = 3
FILE_PREVIEW_DPI = 96
PDF_PRINT_DPI = 200
MAX_FILE_TEXT_CHARS = 50000
FILE_TTL_SECONDS = 1800
MAX_PREPARED_FILES = 20

# 打印任务结束后，临时文件保留多少秒再删除。
DELETE_AFTER_SECONDS = 30

# 登录状态保留天数（电脑重启后需要重新输入密码）。
SESSION_LIFETIME_DAYS = 30

# =============================================================================
# 二、基础环境检查与依赖导入
# =============================================================================

if os.name != "nt":
    print("本程序只支持 Windows 系统，请在 Windows 电脑上运行。")
    raise SystemExit(1)

if sys.version_info < (3, 9):
    print("Python 版本太旧，请安装 Python 3.10 或更高版本后再运行。")
    raise SystemExit(1)

# 依赖检查用 find_spec（只查存在、不真正加载），PDF 和 Office COM 这类大块头
# 改成打印时按需加载，能明显缩短从双击运行到“可以扫码”的等待时间。
_REQUIRED_MODULES = (
    "flask", "PIL", "qrcode", "werkzeug",
    "pypdfium2", "win32print", "win32ui", "win32con", "win32com", "pythoncom",
)
_missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
if _missing:
    print("缺少运行依赖：" + "、".join(_missing))
    print("请在命令行执行：")
    print('pip install -i https://mirrors.aliyun.com/pypi/simple/ Flask "pywin32>=306" "Pillow>=10.0" "qrcode[pil]>=7.4" "pypdfium2>=4.0"')
    print("也可以执行：pip install -r requirements.txt")
    raise SystemExit(1)

# 启动关键路径只导入必需的库。
import flask  # noqa: E402
from flask import (  # noqa: E402
    Flask,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageWin  # noqa: E402
import qrcode  # noqa: E402
import win32con  # noqa: E402
import win32print  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

# 懒加载登记表：变量名 -> 模块名。首次使用时才真正 import。
_LAZY_MODULES = {
    "pdfium": "pypdfium2",
    "pythoncom": "pythoncom",
    "win32": "win32com.client",
    "win32ui": "win32ui",
}


def _lazy_import(global_name: str):
    """按需加载大模块，加载一次后写回全局变量。"""

    module = importlib.import_module(_LAZY_MODULES[global_name])
    globals()[global_name] = module
    return module

# 程序目录：二维码和 temp_print 都放在这里，和当前命令行所在目录无关。
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / TEMP_DIR_NAME
QR_WALL_PATH = BASE_DIR / QR_WALL_FILENAME

# 用 pythonw.exe 开机自启时没有控制台，sys.stdout / sys.stderr 可能是 None。
# 先接到空设备，避免 print / logging 在后台运行时出错。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# 运行时由 main() 填写。
LAN_IP = ""
LAN_URL = ""
QR_PNG_BYTES = b""  # 贴墙用 300 DPI 高清二维码（后台生成，不阻塞启动）
WEB_QR_PNG_BYTES = b""  # 网页展示用的小尺寸二维码（启动关键路径，几毫秒）
WEB_QR_ETAG = ""

# 控制台日志。前端永远看不到这里的错误堆栈。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("family_print")

# 关闭 Flask 自带的英文启动横幅，让控制台只显示中文关键信息。
try:
    import flask.cli

    flask.cli.show_server_banner = lambda *args, **kwargs: None
except Exception:
    pass


# =============================================================================
# 三、通用小工具
# =============================================================================


def mask_password(password: str) -> str:
    """控制台脱敏展示密码，只显示首尾字符。"""
    if not password:
        return "（未设置）"
    if len(password) == 1:
        return "*"
    if len(password) == 2:
        return password[0] + "*"
    return password[0] + "*" * (len(password) - 2) + password[-1]


def _valid_ipv4(value: str) -> bool:
    """判断是不是可用的局域网 IPv4。"""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip.is_loopback or ip.is_unspecified or ip.is_link_local:
        return False
    if ip.is_multicast or ip.is_reserved:
        return False
    return True


def detect_lan_ip() -> str:
    """
    自动获取本机 IPv4 内网地址。

    优先使用系统访问外网时选择的网卡地址；再参考主机名解析结果。
    如果电脑有多张网卡，可用顶部 LAN_IP_OVERRIDE 手工指定。
    """
    override = LAN_IP_OVERRIDE.strip()
    if override:
        if _valid_ipv4(override):
            return override
        log.warning("LAN_IP_OVERRIDE 不是有效的 IPv4，已忽略：%s", override)

    candidates: list[str] = []

    # UDP connect 不会真的发包，只是让系统选出当前默认网卡的地址。
    for target in ("8.8.8.8", "114.114.114.114", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.8)
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if _valid_ipv4(ip):
                candidates.append(ip)
        except OSError:
            pass
        finally:
            sock.close()

    # 补充主机名解析出来的地址，适用于部分特殊网络环境。
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(
            hostname, None, socket.AF_INET, socket.SOCK_STREAM
        ):
            ip = info[4][0]
            if _valid_ipv4(ip):
                candidates.append(ip)
    except OSError:
        pass

    # 去重，并优先选择常见的家庭局域网网段。
    unique: list[str] = []
    for ip in candidates:
        if ip not in unique:
            unique.append(ip)

    def score(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                if 16 <= second <= 31:
                    return 2
            except (ValueError, IndexError):
                pass
        return 3

    unique.sort(key=score)
    return unique[0] if unique else "127.0.0.1"


def build_qr_png(data: str, box_size: int = 32) -> bytes:
    """生成黑白二维码 PNG。box_size 越大图越高清，编码也越慢。"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    if hasattr(image, "get_image"):
        image = image.get_image()
    image = image.convert("RGB")

    buffer = io.BytesIO()
    if box_size >= 32:
        # 300 DPI 的 PNG，打印出来边缘清晰，微信也容易识别。
        image.save(buffer, format="PNG", dpi=(300, 300))
    else:
        image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _px_from_mm(mm: float, dpi: int) -> int:
    return int(round(mm / 25.4 * dpi))


def _page_size_px(dpi: int) -> tuple[int, int]:
    """A4 纵向页面像素尺寸。"""
    return _px_from_mm(210.0, dpi), _px_from_mm(297.0, dpi)


# =============================================================================
# 四、文字排版与 A4 页面渲染
# =============================================================================

# 把英文单词/网址当成整体，中文按单字换行。这样比逐字符硬切更自然。
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[._@:/?#&=+%\-][A-Za-z0-9]+)*|[ \t]+|.", re.S
)


@lru_cache(maxsize=4)
def _load_text_font(size_px: int) -> ImageFont.FreeTypeFont:
    """加载宋体；个别精简版 Windows 没有宋体时，自动改用其他中文字体。"""
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    font_names = (
        "simsun.ttc",   # 宋体（首选）
        "simsunb.ttf",  # 宋体粗体
        "msyh.ttc",     # 微软雅黑（兜底）
        "msyhbd.ttc",
        "Deng.ttf",     # 等线（兜底）
        "simhei.ttf",   # 黑体（兜底）
    )
    errors: list[str] = []
    for name in font_names:
        path = windir / "Fonts" / name
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size_px, index=0)
        except OSError as exc:
            errors.append(name + ": " + str(exc))

    detail = "；".join(errors) if errors else "Windows 字体目录中没有找到宋体"
    raise RuntimeError("无法加载中文字体：" + detail)


def _wrap_paragraph(
    paragraph: str, font: ImageFont.FreeTypeFont, max_width: float
) -> list[str]:
    """按可用宽度把一段文字拆成多行，保留空行。"""
    if paragraph == "":
        return [""]

    tokens = _TOKEN_RE.findall(paragraph)
    lines: list[str] = []
    current = ""

    def append_long_token(token: str) -> None:
        """一个英文长单词/长网址比整行还宽时，按字符强行拆开。"""
        nonlocal current
        for char in token:
            if current and font.getlength(current + char) > max_width:
                lines.append(current.rstrip())
                current = char
            else:
                current += char

    for token in tokens:
        if token.isspace():
            if not current:
                continue
            candidate = current + token
            if font.getlength(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current.rstrip())
                current = ""
            continue

        token_width = font.getlength(token)

        if not current:
            # 行首遇到超长英文单词/网址时，也要拆开，不能让它冲出纸张。
            if token_width <= max_width:
                current = token
            else:
                append_long_token(token)
            continue

        candidate = current + token
        if font.getlength(candidate) <= max_width:
            current = candidate
            continue

        # 当前行放不下了，先收行；如果这个英文长单词本身超宽，再按字符拆。
        lines.append(current.rstrip())
        current = ""
        if token_width <= max_width:
            current = token
        else:
            append_long_token(token)

    if current or not lines:
        lines.append(current.rstrip())
    return lines


def _iter_text_page_images(
    text: str, dpi: int = RENDER_DPI
) -> Iterator[Image.Image]:
    """
    把文字排成一张张 A4 白底黑字页面，逐页交给打印函数。

    注意：这个函数本身不是生成器（真正的 yield 在内层 generate 里），
    所以字体加载、换行等准备工作会在 StartDoc 之前完成；准备工作失败
    不会留下一个空打印作业。dpi 参数用于打印或手机预览。
    """
    dpi = max(48, int(dpi))
    font_size_px = max(1, int(round(TEXT_FONT_SIZE_PT * dpi / 72.0)))
    font = _load_text_font(font_size_px)
    line_height = max(1, int(round(font_size_px * TEXT_LINE_SPACING)))

    page_width, page_height = _page_size_px(dpi)
    margin = _px_from_mm(PAGE_MARGIN_MM, dpi)
    text_width = page_width - margin * 2
    bottom_limit = page_height - margin

    normalized = (
        text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    )
    all_lines: list[str] = []
    for paragraph in normalized.split("\n"):
        all_lines.extend(_wrap_paragraph(paragraph, font, text_width))

    def generate() -> Iterator[Image.Image]:
        page: Optional[Image.Image] = None
        draw: Optional[ImageDraw.ImageDraw] = None
        y = margin
        has_content = False

        for line in all_lines:
            if page is None or y + line_height > bottom_limit:
                if page is not None and has_content:
                    yield page
                page = Image.new("RGB", (page_width, page_height), "white")
                draw = ImageDraw.Draw(page)
                y = margin
                has_content = False

            if line:
                # 左对齐、页边距固定，不提供任何排版选项。
                draw.text((margin, y), line, font=font, fill=(0, 0, 0))
                has_content = True
            y += line_height

        if page is not None and has_content:
            yield page

    return generate()


# =============================================================================
# 五、图片适配 A4 页面
# =============================================================================


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    """透明图铺白底，其他颜色模式统一转 RGB 后打印。"""
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _render_image_page(image_path: Path, dpi: int = RENDER_DPI) -> Image.Image:
    """图片等比例缩放，完整放进 A4 页边距内并居中，不拉伸、不裁切。"""
    with Image.open(image_path) as source:
        source = ImageOps.exif_transpose(source)  # 手机照片方向自动摆正
        source.load()
        image = _flatten_to_rgb(source)

    dpi = max(48, int(dpi))
    page_width, page_height = _page_size_px(dpi)
    margin = _px_from_mm(PAGE_MARGIN_MM, dpi)
    max_width = page_width - margin * 2
    max_height = page_height - margin * 2

    fitted = ImageOps.contain(
        image, (max_width, max_height), Image.Resampling.LANCZOS
    )
    canvas = Image.new("RGB", (page_width, page_height), "white")
    x = (page_width - fitted.width) // 2
    y = (page_height - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


# =============================================================================
# 五-B、手机文件 / 微信聊天文件
# =============================================================================

PDF_EXTS = {".pdf"}
WORD_EXTS = {".doc", ".docx", ".rtf", ".odt"}
EXCEL_EXTS = {".xls", ".xlsx", ".csv", ".ods"}
PPT_EXTS = {".ppt", ".pptx", ".odp"}
TEXT_FILE_EXTS = {".txt", ".md", ".log", ".html", ".htm", ".json"}
OFFICE_EXTS = WORD_EXTS | EXCEL_EXTS | PPT_EXTS

_OFFICE_LOCK = threading.Lock()


class UserFacingError(Exception):
    """可以安全展示给前端的文件处理错误，不包含堆栈和英文错误。"""


class _HTMLTextExtractor(HTMLParser):
    """从聊天记录导出的 HTML 中提取可打印文字，忽略脚本和样式。"""

    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "dl", "dt",
        "dd", "fieldset", "figcaption", "figure", "footer", "form", "h1",
        "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
        "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def get_text(self) -> str:
        text = html_lib.unescape("".join(self.parts))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\x00", "")
        lines = [line.strip() for line in text.split("\n")]
        result: list[str] = []
        blank = 0
        for line in lines:
            if line:
                blank = 0
                result.append(line)
            else:
                blank += 1
                if blank <= 2:
                    result.append("")
        return "\n".join(result).strip()


def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UserFacingError("这个文字文件编码无法识别，请另存为 UTF-8 后再试")


def _to_preview_data_uri(image: Image.Image) -> str:
    preview = image.convert("RGB")
    preview.thumbnail((720, 1020), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=82, optimize=True)
    return (
        "data:image/jpeg;base64,"
        + base64.b64encode(buffer.getvalue()).decode("ascii")
    )


def _render_pdf_page(pdf, page_index: int, dpi: int) -> Image.Image:
    page = pdf[page_index]
    width, height = page.get_size()
    max_side = 5000.0
    scale = max(0.2, float(dpi) / 72.0)
    if max(width, height) * scale > max_side:
        scale = max_side / max(width, height)
    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil().convert("RGB")
    finally:
        try:
            bitmap.close()
        except Exception:
            pass
    return image


def _pdf_preview(pdf_path: Path):
    """返回 (总页数, 前几页预览 data URI)。"""
    pdfium = _lazy_import("pdfium")
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        raise UserFacingError("PDF 文件损坏或无法打开") from exc

    try:
        page_count = len(pdf)
        if page_count <= 0:
            raise UserFacingError("这个 PDF 没有可打印的页面")
        previews = []
        for index in range(min(FILE_PREVIEW_PAGES, page_count)):
            previews.append(
                _to_preview_data_uri(
                    _render_pdf_page(pdf, index, FILE_PREVIEW_DPI)
                )
            )
        return page_count, previews
    finally:
        pdf.close()


def _iter_pdf_page_images(pdf_path: Path) -> Iterator[Image.Image]:
    pdfium = _lazy_import("pdfium")
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(pdf) <= 0:
            raise RuntimeError("这个 PDF 没有可打印的页面")
        for index in range(len(pdf)):
            yield _render_pdf_page(pdf, index, PDF_PRINT_DPI)
    finally:
        pdf.close()


def _print_pdf_job(pdf_path: Path) -> None:
    _print_page_images(
        _iter_pdf_page_images(pdf_path), "家庭打印-文件"
    )


def _print_slide_job(slides_dir: Path) -> None:
    slide_files = sorted(slides_dir.glob("slide_*.png"))
    if not slide_files:
        raise RuntimeError("没有可打印的幻灯片")
    pages = (_render_image_page(path) for path in slide_files)
    _print_page_images(pages, "家庭打印-演示文稿")


def _ppt_to_images(source: Path, work_dir: Path) -> Path:
    """
    PowerPoint 的 PDF 导出在部分 Office 安装上会失败，这里改用
    Slide.Export 把每一页导出成 PNG，再走统一的 A4 图片打印链路。
    版式仍然保持原样，只是不再依赖 PPT 的 PDF 导出组件。
    """
    slides_dir = work_dir / "slides"
    slides_dir.mkdir(parents=True, exist_ok=False)
    with _OFFICE_LOCK:
        _lazy_import("pythoncom").CoInitialize()
        _lazy_import("win32")
        app = None
        presentation = None
        try:
            app = win32.DispatchEx("PowerPoint.Application")
            presentation = app.Presentations.Open(
                str(source),
                ReadOnly=True,
                Untitled=False,
                WithWindow=False,
            )
            slide_width = float(presentation.PageSetup.SlideWidth)
            slide_height = float(presentation.PageSetup.SlideHeight)
            if slide_width <= 0 or slide_height <= 0:
                raise UserFacingError("无法读取演示文稿的页面尺寸")
            export_width = 2400
            export_height = max(1, int(round(export_width * slide_height / slide_width)))
            count = int(presentation.Slides.Count)
            if count <= 0:
                raise UserFacingError("这个演示文稿没有可打印的页面")
            for index in range(1, count + 1):
                slide = presentation.Slides(index)
                target = slides_dir / f"slide_{index:04d}.png"
                slide.Export(str(target), "PNG", export_width, export_height)
            presentation.Close()
            presentation = None
        except UserFacingError:
            raise
        except Exception as exc:
            log.exception("PPT 导出图片失败")
            raise UserFacingError(
                "无法打开这个 PowerPoint 文件。请确认电脑装有 PowerPoint，"
                "或者先把文件另存为 PDF 再上传。"
            ) from exc
        finally:
            if presentation is not None:
                try:
                    presentation.Close()
                except Exception:
                    pass
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
    if not any(slides_dir.glob("slide_*.png")):
        raise UserFacingError("PowerPoint 文件转换失败，请先另存为 PDF 后再试")
    return slides_dir



def _office_to_pdf(source: Path, target: Path, extension: str) -> None:
    """
    用本机安装的 Microsoft Office 把 Word/Excel 转成 PDF。

    这样打印时保留原排版，再由 PDF 渲染成 A4 页面预览/打印。
    没有安装 Office 时给前端返回统一中文提示。
    """
    with _OFFICE_LOCK:
        _lazy_import("pythoncom").CoInitialize()
        _lazy_import("win32")
        app = None
        document = None
        try:
            if extension in WORD_EXTS:
                app = win32.DispatchEx("Word.Application")
                app.Visible = False
                app.DisplayAlerts = 0
                app.AutomationSecurity = 3
                document = app.Documents.Open(
                    str(source), ReadOnly=True, AddToRecentFiles=False
                )
                document.ExportAsFixedFormat(str(target), 17)  # PDF
                document.Close(False)
                document = None
            elif extension in EXCEL_EXTS:
                app = win32.DispatchEx("Excel.Application")
                app.Visible = False
                app.DisplayAlerts = False
                app.AutomationSecurity = 3
                document = app.Workbooks.Open(
                    str(source), ReadOnly=True, UpdateLinks=0
                )
                document.ExportAsFixedFormat(0, str(target))  # PDF
                document.Close(False)
                document = None
            else:
                raise UserFacingError("这个 Office 文件类型暂不支持")
        except UserFacingError:
            raise
        except Exception as exc:
            log.exception("Office 转 PDF 失败")
            raise UserFacingError(
                "无法打开这个 Office 文件。请确认电脑装有 Word/Excel，"
                "或者先把文件另存为 PDF 再上传。"
            ) from exc
        finally:
            if document is not None:
                try:
                    document.Close(False)
                except Exception:
                    pass
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    if not target.exists() or target.stat().st_size == 0:
        raise UserFacingError("Office 文件转换失败，请先另存为 PDF 后再试")


@dataclass
class PreparedFile:
    file_id: str
    name: str
    kind: str  # pdf / text / image / images
    page_count: int
    print_path: Path
    work_dir: Path
    created_at: float = field(default_factory=time.time)


def _prepare_upload(upload) -> tuple[PreparedFile, list[str]]:
    """保存并解析手机文件，生成前几页手机预览，返回待打印记录。"""
    original_name = Path(upload.filename or "未命名文件").name
    extension = Path(original_name).suffix.lower()
    supported = (
        PDF_EXTS
        | OFFICE_EXTS
        | TEXT_FILE_EXTS
        | IMAGE_EXTS
    )
    if extension not in supported:
        raise UserFacingError(
            "暂不支持这种文件格式。请转成 PDF、Word、Excel、PPT、"
            "TXT、HTML 或图片后再上传。"
        )

    file_id = uuid.uuid4().hex
    work_dir = TEMP_DIR / ("file_" + file_id)
    work_dir.mkdir(parents=True, exist_ok=False)
    source = work_dir / ("source" + extension)

    try:
        upload.save(str(source))
        size = source.stat().st_size
        if size <= 0:
            raise UserFacingError("这个文件是空的")
        if size > FILE_MAX_BYTES:
            raise UserFacingError("文件太大了，请上传 50MB 以内的文件")
        if extension in IMAGE_EXTS and size > IMAGE_MAX_BYTES:
            raise UserFacingError("图片请小于 8MB")

        if extension in PDF_EXTS:
            page_count, previews = _pdf_preview(source)
            return (
                PreparedFile(
                    file_id=file_id,
                    name=original_name,
                    kind="pdf",
                    page_count=page_count,
                    print_path=source,
                    work_dir=work_dir,
                ),
                previews,
            )

        if extension in PPT_EXTS:
            slides_dir = _ppt_to_images(source, work_dir)
            slide_files = sorted(slides_dir.glob("slide_*.png"))
            previews = [
                _to_preview_data_uri(
                    _render_image_page(path, FILE_PREVIEW_DPI)
                )
                for path in slide_files[:FILE_PREVIEW_PAGES]
            ]
            return (
                PreparedFile(
                    file_id=file_id,
                    name=original_name,
                    kind="images",
                    page_count=len(slide_files),
                    print_path=slides_dir,
                    work_dir=work_dir,
                ),
                previews,
            )

        if extension in OFFICE_EXTS:
            pdf_path = work_dir / "converted.pdf"
            _office_to_pdf(source, pdf_path, extension)
            page_count, previews = _pdf_preview(pdf_path)
            return (
                PreparedFile(
                    file_id=file_id,
                    name=original_name,
                    kind="pdf",
                    page_count=page_count,
                    print_path=pdf_path,
                    work_dir=work_dir,
                ),
                previews,
            )

        if extension in IMAGE_EXTS:
            try:
                with Image.open(source) as image:
                    actual_format = image.format
                    width, height = image.size
                    if width * height > MAX_IMAGE_PIXELS:
                        raise UserFacingError("图片尺寸太大了")
                    image.verify()
            except UserFacingError:
                raise
            except Exception as exc:
                raise UserFacingError("图片文件损坏或无法识别") from exc
            if actual_format not in {"JPEG", "PNG", "WEBP"}:
                raise UserFacingError("图片只支持 jpg、png、webp")
            preview = _render_image_page(source, FILE_PREVIEW_DPI)
            return (
                PreparedFile(
                    file_id=file_id,
                    name=original_name,
                    kind="image",
                    page_count=1,
                    print_path=source,
                    work_dir=work_dir,
                ),
                [_to_preview_data_uri(preview)],
            )

        # 文字 / 聊天记录 / HTML
        raw = _read_text_file(source)
        if extension in {".html", ".htm"}:
            parser = _HTMLTextExtractor()
            parser.feed(raw)
            text = parser.get_text()
        else:
            text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
            if extension == ".json":
                try:
                    text = json.dumps(
                        json.loads(text), ensure_ascii=False, indent=2
                    )
                except Exception:
                    pass

        if not text:
            raise UserFacingError("这个文件里没有可打印的文字")
        if len(text) > MAX_FILE_TEXT_CHARS:
            raise UserFacingError(
                "文件文字太多，最多支持 5 万字，请拆分后再打印"
            )

        text_path = work_dir / "content.txt"
        text_path.write_text(text, encoding="utf-8")
        page_count = 0
        previews: list[str] = []
        for page in _iter_text_page_images(text, FILE_PREVIEW_DPI):
            page_count += 1
            if len(previews) < FILE_PREVIEW_PAGES:
                previews.append(_to_preview_data_uri(page))
        if page_count <= 0:
            raise UserFacingError("这个文件里没有可打印的文字")
        return (
            PreparedFile(
                file_id=file_id,
                name=original_name,
                kind="text",
                page_count=page_count,
                print_path=text_path,
                work_dir=work_dir,
            ),
            previews,
        )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


_prepared_lock = threading.Lock()
_prepared_files: dict[str, PreparedFile] = {}


def _delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _schedule_prepared_cleanup(file_id: str, work_dir: Path) -> None:
    def cleanup() -> None:
        with _prepared_lock:
            record = _prepared_files.pop(file_id, None)
        if record is not None:
            _delete_path(work_dir)
            log.info("已清理未打印的上传文件：%s", record.name)

    timer = threading.Timer(FILE_TTL_SECONDS, cleanup)
    timer.daemon = True
    timer.start()


def _store_prepared(record: PreparedFile) -> None:
    old_dirs: list[Path] = []
    with _prepared_lock:
        _prepared_files[record.file_id] = record
        while len(_prepared_files) > MAX_PREPARED_FILES:
            _, old = next(iter(_prepared_files.items()))
            _prepared_files.pop(old.file_id, None)
            old_dirs.append(old.work_dir)
    for old_dir in old_dirs:
        _delete_path(old_dir)
    _schedule_prepared_cleanup(record.file_id, record.work_dir)


def _take_prepared(file_id: str) -> Optional[PreparedFile]:
    with _prepared_lock:
        return _prepared_files.pop(file_id, None)



# =============================================================================
# 六、Windows 默认打印机调用
# =============================================================================

# Windows 打印机状态位。驱动/系统不一定上报全部状态，这里只做打印前尽力检查。
# 顺序就是排查优先级：缺纸、卡纸、离线最常见，放在前面。
_PRINTER_BAD_STATUS = (
    (0x00000010, "打印机缺纸", "paper_out"),
    (0x00000008, "打印机卡纸", "paper_jam"),
    (0x00000080, "打印机离线", "offline"),
    (0x00400000, "打印机盖未关好", "door_open"),
    (0x00040000, "打印机缺墨或墨粉", "no_toner"),
    (0x00000001, "打印机已暂停", "paused"),
    (0x00000002, "打印机报错", "printer_error"),
    (0x00000040, "打印机纸张故障", "paper_problem"),
    (0x00000800, "打印机出纸口已满", "output_full"),
    (0x00001000, "打印机不可用", "not_available"),
    (0x02000000, "打印机服务器离线", "server_offline"),
    (0x00080000, "打印机无法打印当前页", "page_punt"),
    (0x00100000, "打印机需要人工处理", "user_intervention"),
    (0x00200000, "打印机内存不足", "out_of_memory"),
    (0x00000004, "打印任务异常", "pending_deletion"),
)
# 注意：0x00000200（忙）、0x00000400（正在打印）、0x00800000（状态未知）、
# 0x01000000（省电休眠）属于正常/未知状态，不能当故障。
# 这里只列会阻止打印的状态位（依据 Windows winspool 的 PRINTER_STATUS_* 定义）。


class PrinterUnavailable(RuntimeError):
    """默认打印机不可用，带一个前端可映射的稳定原因代码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _get_default_printer() -> Optional[str]:
    try:
        name = win32print.GetDefaultPrinter()
        return name or None
    except Exception:
        return None


def _printer_status_problem(printer_name: str) -> Optional[tuple[str, str]]:
    """读取默认打印机状态；读取失败不阻止打印，只记录日志。"""
    handle = None
    try:
        handle = win32print.OpenPrinter(printer_name)
        info = win32print.GetPrinter(handle, 2)
        status = int(info.get("Status", 0) or 0)
    except Exception as exc:
        log.warning("读取打印机状态失败，将继续尝试打印：%s", exc)
        return None
    finally:
        if handle:
            try:
                win32print.ClosePrinter(handle)
            except Exception:
                pass

    for bit, text, code in _PRINTER_BAD_STATUS:
        if status & bit:
            return code, text
    return None


def _get_ready_printer() -> str:
    """取默认打印机并做基本状态检查，异常统一抛给后台打印线程。"""
    printer_name = _get_default_printer()
    if not printer_name:
        raise PrinterUnavailable("no_printer", "没有设置默认打印机")
    problem = _printer_status_problem(printer_name)
    if problem:
        code, text = problem
        raise PrinterUnavailable(code, text)
    return printer_name


def _print_page_images(images: Iterable[Image.Image], job_name: str) -> None:
    """
    串行打印一组 A4 页面。
    使用 Windows 打印 API（pywin32）调用默认打印机；本函数一次只处理一个任务，
    由后台单线程队列保证不会并发打印。
    """
    printer_name = _get_ready_printer()

    _lazy_import("win32ui")
    hdc = win32ui.CreateDC()
    try:
        hdc.CreatePrinterDC(printer_name)
        printable_width = int(hdc.GetDeviceCaps(win32con.HORZRES))
        printable_height = int(hdc.GetDeviceCaps(win32con.VERTRES))
        if printable_width <= 0 or printable_height <= 0:
            raise RuntimeError("无法获取打印机可打印区域")

        started = False
        completed = False
        printed_pages = 0
        try:
            hdc.StartDoc(job_name)
            started = True

            for image in images:
                if image.mode != "RGB":
                    image = image.convert("RGB")

                hdc.StartPage()
                try:
                    # 保持 A4 页面比例，完整放进打印机可打印区域并居中。
                    scale = min(
                        printable_width / float(image.width),
                        printable_height / float(image.height),
                    )
                    draw_width = max(1, int(image.width * scale))
                    draw_height = max(1, int(image.height * scale))
                    x = max(0, (printable_width - draw_width) // 2)
                    y = max(0, (printable_height - draw_height) // 2)

                    dib = ImageWin.Dib(image)
                    dib.draw(
                        hdc.GetHandleOutput(),
                        (x, y, x + draw_width, y + draw_height),
                    )
                finally:
                    hdc.EndPage()
                printed_pages += 1

            if printed_pages == 0:
                raise RuntimeError("没有可打印的内容")
            completed = True
        finally:
            if started:
                if completed:
                    # EndDoc 失败也必须让任务变成失败状态，不能吞掉异常。
                    hdc.EndDoc()
                else:
                    # 中途失败时取消整个作业，避免只打印半份内容。
                    try:
                        hdc.AbortDoc()
                    except Exception:
                        log.exception("取消打印任务时出错")
    finally:
        try:
            hdc.DeleteDC()
        except Exception:
            pass


def _print_text_job(txt_path: Path) -> None:
    """从临时 txt 读取文字，排版后调用默认打印机。"""
    text = txt_path.read_text(encoding="utf-8")
    _print_page_images(_iter_text_page_images(text), "家庭打印-文字")


def _print_image_job(image_path: Path) -> None:
    """把上传图片排版到 A4 后调用默认打印机。"""
    page = _render_image_page(image_path)
    _print_page_images(iter([page]), "家庭打印-图片")


# =============================================================================
# 七、打印队列、任务状态与临时文件清理
# =============================================================================


@dataclass
class PrintJob:
    task_id: str
    kind: str  # "text" / "image" / "pdf"
    file_path: Path
    cleanup_paths: tuple[Path, ...] = ()


PRINT_QUEUE: "queue.Queue[PrintJob]" = queue.Queue()

_STATUS_LOCK = threading.Lock()
_TASK_STATUS: dict[str, tuple[str, str, float]] = {}


def _set_task_status(task_id: str, status: str, reason: str = "") -> None:
    with _STATUS_LOCK:
        _TASK_STATUS[task_id] = (status, reason, time.time())
        # 状态只用于页面轮询，保留最近 300 个，避免长期运行占内存。
        while len(_TASK_STATUS) > 300:
            _TASK_STATUS.pop(next(iter(_TASK_STATUS)))


def _get_task_state(task_id: str) -> tuple[str, str]:
    with _STATUS_LOCK:
        item = _TASK_STATUS.get(task_id)
    return (item[0], item[1]) if item else ("unknown", "")


def _schedule_temp_cleanup(
    file_path: Path, cleanup_paths: tuple[Path, ...] = ()
) -> None:
    """任务结束（成功/失败）30 秒后删除临时文件或整个文件工作目录。"""

    def remove() -> None:
        paths = [file_path, *cleanup_paths]
        seen: set[str] = set()
        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            _delete_path(path)
        log.info("已清理临时文件：%s", file_path.name)

    timer = threading.Timer(DELETE_AFTER_SECONDS, remove)
    timer.daemon = True
    timer.start()


def _print_worker() -> None:
    """后台唯一消费者：严格串行打印，禁止并发。"""
    log.info("打印队列已启动，所有任务将排队依次打印。")
    while True:
        job = PRINT_QUEUE.get()
        try:
            _set_task_status(job.task_id, "printing")
            log.info("开始打印任务 %s（%s）", job.task_id[:8], job.kind)
            if job.kind == "text":
                _print_text_job(job.file_path)
            elif job.kind == "image":
                _print_image_job(job.file_path)
            elif job.kind == "pdf":
                _print_pdf_job(job.file_path)
            elif job.kind == "images":
                _print_slide_job(job.file_path)
            else:
                raise RuntimeError("未知的打印任务类型")
            _set_task_status(job.task_id, "done")
            log.info("打印任务完成 %s", job.task_id[:8])
        except PrinterUnavailable as exc:
            _set_task_status(job.task_id, "error", exc.code)
            # 具体原因只用于前端中文提示；控制台保留完整日志。
            log.error("打印失败（%s）：%s", exc.code, exc)
            log.error("%s", traceback.format_exc())
        except Exception as exc:
            _set_task_status(job.task_id, "error", "printer_error")
            # 只写控制台，前端只看到中文原因和解决办法。
            log.error("打印失败：%s", exc)
            log.error("%s", traceback.format_exc())
        finally:
            try:
                _schedule_temp_cleanup(job.file_path, job.cleanup_paths)
            except Exception:
                log.exception("安排临时文件清理失败")
            PRINT_QUEUE.task_done()


# =============================================================================
# 八、Flask 网页
# =============================================================================

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config.update(
    PROPAGATE_EXCEPTIONS=False,
    MAX_CONTENT_LENGTH=max(IMAGE_MAX_BYTES, FILE_MAX_BYTES) + 2 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_LIFETIME_DAYS),
    SESSION_COOKIE_NAME="family_print_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # 家庭内网是 http，不能设 True
)
try:
    app.json.ensure_ascii = False
except Exception:
    pass


@lru_cache(maxsize=4)
def _compiled_template(source: str):
    """预编译 Jinja 模板：页面 HTML 只在首次访问时解析一次。"""

    return app.jinja_env.from_string(source)


UNLOCK_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#060911">
<title>家庭打印 · 输入密码</title>
<script>(function(){try{var t=localStorage.getItem("fp-theme");document.documentElement.setAttribute("data-theme",(t==="light"||t==="dark")?t:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"));}catch(e){document.documentElement.setAttribute("data-theme","dark");}})();</script>
<style>
:root{
  --bg:#060911;
  --surface:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.03));
  --surface-2:rgba(255,255,255,.055);
  --surface-flat:rgba(255,255,255,.04);
  --line:rgba(255,255,255,.10);
  --line-2:rgba(255,255,255,.18);
  --text:#EAF0FA;
  --muted:#98A5BA;
  --faint:#6B7789;
  --text-dim:rgba(234,240,250,.48);
  --accent:#6C8CFF;
  --grad:linear-gradient(135deg,#5B7CFF 0%,#8B6DFF 55%,#B06CFF 100%);
  --bad:#FB7185;
  --bad-bg:rgba(251,113,133,.10);
  --bad-line:rgba(251,113,133,.34);
  --ring:rgba(108,140,255,.55);
  --shadow:0 40px 90px -40px rgba(0,0,0,.95);
  --ease:cubic-bezier(.16,1,.3,1);
  --spring:cubic-bezier(.22,1.2,.36,1);
  color-scheme:dark;
}
:root[data-theme="light"]{
  --bg:#F3F5FA;
  --surface:linear-gradient(180deg,rgba(255,255,255,.98),rgba(255,255,255,.90));
  --surface-2:rgba(15,23,42,.045);
  --surface-flat:#fff;
  --line:rgba(15,23,42,.10);
  --line-2:rgba(15,23,42,.18);
  --text:#0B1220;
  --muted:#55617A;
  --faint:#7C879B;
  --text-dim:rgba(11,18,32,.50);
  --accent:#3D5CE5;
  --grad:linear-gradient(135deg,#4361EE 0%,#6C4BE8 55%,#8B4BE8 100%);
  --bad:#D93A52;
  --bad-bg:rgba(217,58,82,.08);
  --bad-line:rgba(217,58,82,.28);
  --ring:rgba(67,97,238,.40);
  --shadow:0 40px 80px -44px rgba(15,23,42,.40);
  color-scheme:light;
}
*,*::before,*::after{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg)}
body{
  margin:0;min-height:100dvh;display:grid;place-items:center;
  padding:24px 18px calc(24px + env(safe-area-inset-bottom));
  color:var(--text);
  font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC","Hiragino Sans GB",system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:16px;line-height:1.6;-webkit-text-size-adjust:100%;overflow-x:hidden;
}
button{font:inherit;color:inherit;border:0;background:none;cursor:pointer}
h1,p{margin:0}

.aurora{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.aurora span{position:absolute;border-radius:50%;will-change:transform;transform:translateZ(0)}
.aurora .g1{width:130vw;height:130vw;left:-46vw;top:-62vw;background:radial-gradient(circle,rgba(91,124,255,.34) 0%,rgba(91,124,255,0) 62%);animation:drift1 30s var(--ease) infinite alternate}
.aurora .g2{width:110vw;height:110vw;right:-40vw;bottom:-40vw;background:radial-gradient(circle,rgba(155,123,255,.28) 0%,rgba(155,123,255,0) 60%);animation:drift2 34s var(--ease) infinite alternate}
:root[data-theme="light"] .aurora span{opacity:.55}
@keyframes drift1{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(6vw,5vw,0) scale(1.08)}}
@keyframes drift2{from{transform:translate3d(0,0,0) scale(1.06)}to{transform:translate3d(-6vw,-5vw,0) scale(1)}}
@keyframes riseIn{from{opacity:0;transform:translateY(14px) scale(.985)}to{opacity:1;transform:none}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes shake{0%,100%{transform:translateX(0)}20%{transform:translateX(-6px)}40%{transform:translateX(5px)}60%{transform:translateX(-3px)}80%{transform:translateX(2px)}}

.wrap{position:relative;z-index:1;width:100%;max-width:420px}
.theme-fab{position:fixed;top:16px;right:16px;z-index:5;width:36px;height:36px;border-radius:11px;display:grid;place-items:center;
  background:var(--surface-flat);border:1px solid var(--line);color:var(--muted);
  transition:transform .22s var(--spring),color .2s,border-color .2s}
.theme-fab:hover{color:var(--text);border-color:var(--line-2)}
.theme-fab:active{transform:scale(.92)}
.theme-fab svg{width:17px;height:17px}
.theme-fab .moon{display:none}
:root[data-theme="light"] .theme-fab .sun{display:none}
:root[data-theme="light"] .theme-fab .moon{display:block}

.card{
  position:relative;padding:32px 26px 28px;border-radius:26px;
  background:var(--surface);border:1px solid var(--line);
  box-shadow:var(--shadow);
  -webkit-backdrop-filter:blur(18px) saturate(150%);
  backdrop-filter:blur(18px) saturate(150%);
  animation:riseIn .62s var(--ease) both;
}
.card::before{
  content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:linear-gradient(180deg,rgba(255,255,255,.10),transparent 40%);opacity:.7;
}
.mark{
  width:60px;height:60px;border-radius:19px;display:grid;place-items:center;margin:0 auto;
  background:var(--grad);
  box-shadow:0 20px 40px -20px rgba(108,140,255,.95),inset 0 1px 0 rgba(255,255,255,.38);
  animation:riseIn .7s var(--spring) .06s both;
}
.mark svg{width:29px;height:29px;color:#fff}
h1{margin-top:20px;font-size:23px;font-weight:800;letter-spacing:.02em;text-align:center;animation:riseIn .6s var(--ease) .12s both}
.sub{margin-top:7px;font-size:13.5px;color:var(--muted);text-align:center;line-height:1.6;animation:riseIn .6s var(--ease) .16s both}

form{margin-top:24px;animation:riseIn .6s var(--ease) .2s both}
.label{display:block;margin-bottom:8px;font-size:13px;font-weight:700;color:var(--muted);letter-spacing:.03em}
.field{position:relative}
input[type="password"],input[type="text"]{
  display:block;width:100%;height:56px;padding:0 52px 0 15px;
  color:var(--text);background:var(--surface-2);
  border:1px solid var(--line);border-radius:14px;
  font-family:inherit;font-size:19px;letter-spacing:.14em;
  transition:border-color .2s,box-shadow .2s,background .2s;
}
input::placeholder{color:var(--faint);letter-spacing:.02em}
input:focus{outline:none;border-color:var(--ring);box-shadow:0 0 0 3px var(--ring);background:transparent}
.eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:42px;height:42px;border-radius:11px;display:grid;place-items:center;color:var(--faint)}
.eye:hover{color:var(--text);background:var(--surface-2)}
.eye svg{width:18px;height:18px}

.btn{
  position:relative;display:flex;align-items:center;justify-content:center;gap:9px;
  width:100%;height:56px;margin-top:16px;border-radius:14px;overflow:hidden;isolation:isolate;
  font-size:16.5px;font-weight:800;letter-spacing:.03em;color:#fff;background:var(--grad);
  box-shadow:0 18px 36px -20px rgba(108,140,255,.95),inset 0 1px 0 rgba(255,255,255,.28);
  transition:transform .22s var(--spring),box-shadow .24s;
}
.btn::after{
  content:"";position:absolute;inset:0;z-index:-1;
  background:linear-gradient(100deg,transparent 20%,rgba(255,255,255,.30) 48%,transparent 72%);
  transform:translateX(-120%);transition:transform .7s var(--ease);
}
.btn:hover::after{transform:translateX(120%)}
.btn:active{transform:scale(.978)}
.btn .spin{width:16px;height:16px;border-radius:50%;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;animation:spin .7s linear infinite;display:none}
.btn.is-busy .spin{display:block}
.btn:focus-visible{outline:2px solid var(--ring);outline-offset:3px}

.error{
  display:flex;align-items:flex-start;gap:9px;margin-top:14px;padding:12px 13px;border-radius:13px;
  color:var(--bad);background:var(--bad-bg);border:1px solid var(--bad-line);
  font-size:14px;font-weight:700;line-height:1.5;
  animation:shake .5s var(--ease) .08s both;
}
.error svg{width:16px;height:16px;flex:0 0 auto;margin-top:2px}
.hint{margin-top:22px;font-size:12.5px;color:var(--faint);text-align:center;line-height:1.7;animation:riseIn .6s var(--ease) .26s both}
.hint b{color:var(--muted);font-weight:700}
@media (max-width:380px){
  .card{padding:26px 20px 24px;border-radius:22px}
  h1{font-size:21px}
}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important}
}
</style>
</head>
<body>
<div class="aurora" aria-hidden="true"><span class="g1"></span><span class="g2"></span></div>

<button class="theme-fab" id="themeToggle" type="button" aria-label="切换深浅色">
  <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
  <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8z"/></svg>
</button>

<div class="wrap">
  <div class="card">
    <div class="mark">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M7 8V4h10v4"/><path d="M7 17H5.5A1.5 1.5 0 0 1 4 15.5v-5A1.5 1.5 0 0 1 5.5 9h13A1.5 1.5 0 0 1 20 10.5v5a1.5 1.5 0 0 1-1.5 1.5H17"/><path d="M7 14h10v6H7z"/>
      </svg>
    </div>
    <h1>家庭打印</h1>
    <p class="sub">输入使用密码，连接家里的打印机</p>

    <form method="post" action="{{ url_for('unlock') }}" id="unlockForm">
      <label class="label" for="password">使用密码</label>
      <div class="field">
        <input id="password" name="password" type="password" inputmode="text"
               autocomplete="current-password" autocapitalize="off" autocorrect="off"
               spellcheck="false" placeholder="请输入密码" autofocus required>
        <button class="eye" id="eye" type="button" aria-label="显示密码">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/></svg>
        </button>
      </div>
      {% if error %}
      <div class="error" role="alert">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.01"/></svg>
        <span>{{ error }}</span>
      </div>
      {% endif %}
      <button class="btn" type="submit" id="submitBtn">
        <span class="spin" aria-hidden="true"></span>
        <span class="lbl">进入打印页面</span>
      </button>
    </form>

    <p class="hint">忘记密码？问家里帮您设置这台电脑的人</p>
  </div>
</div>

<script>
(function () {
  "use strict";
  var root = document.documentElement;
  var btn = document.getElementById("themeToggle");
  function cur() { return root.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function syncMeta() {
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) { m.setAttribute("content", cur() === "light" ? "#F3F5FA" : "#060911"); }
  }
  syncMeta();
  if (btn) {
    btn.addEventListener("click", function () {
      var next = cur() === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("fp-theme", next); } catch (e) {}
      syncMeta();
    });
  }

  var input = document.getElementById("password");
  var eye = document.getElementById("eye");
  if (eye && input) {
    eye.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      eye.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
      input.focus();
    });
  }

  var form = document.getElementById("unlockForm");
  var submit = document.getElementById("submitBtn");
  if (form && submit) {
    form.addEventListener("submit", function () {
      if (!input.value) { return; }
      submit.classList.add("is-busy");
      var lbl = submit.querySelector(".lbl");
      if (lbl) { lbl.textContent = "正在验证…"; }
      submit.disabled = true;
    });
  }
})();
</script>
</body>
</html>
"""


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<meta name="theme-color" content="#060911">
<title>家庭打印</title>
<script>(function(){try{var t=localStorage.getItem("fp-theme");document.documentElement.setAttribute("data-theme",(t==="light"||t==="dark")?t:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark"));}catch(e){document.documentElement.setAttribute("data-theme","dark");}})();</script>
<style>
/* ============================================================
   设计令牌：一处改色，全站同步
   ============================================================ */
:root{
  --bg:#060911;
  --bg-glow-1:rgba(91,124,255,.30);
  --bg-glow-2:rgba(155,123,255,.24);
  --bg-glow-3:rgba(45,226,197,.14);
  --surface:linear-gradient(180deg,rgba(255,255,255,.062),rgba(255,255,255,.026));
  --surface-flat:rgba(255,255,255,.04);
  --surface-2:rgba(255,255,255,.055);
  --line:rgba(255,255,255,.10);
  --line-2:rgba(255,255,255,.16);
  --text:#EAF0FA;
  --muted:#98A5BA;
  --faint:#6B7789;
  --text-dim:rgba(234,240,250,.48);
  --bg-blur:rgba(6,9,17,.72);
  --accent:#6C8CFF;
  --accent-2:#A78BFA;
  --grad:linear-gradient(135deg,#5B7CFF 0%,#8B6DFF 55%,#B06CFF 100%);
  --ok:#34D399;
  --ok-bg:rgba(52,211,153,.10);
  --ok-line:rgba(52,211,153,.34);
  --bad:#FB7185;
  --bad-bg:rgba(251,113,133,.10);
  --bad-line:rgba(251,113,133,.34);
  --ring:rgba(108,140,255,.55);
  --shadow:0 30px 70px -34px rgba(0,0,0,.95);
  --shadow-sm:0 10px 26px -16px rgba(0,0,0,.9);
  --r-lg:24px; --r-md:16px; --r-sm:12px;
  --ease:cubic-bezier(.16,1,.3,1);
  --spring:cubic-bezier(.22,1.2,.36,1);
  color-scheme:dark;
}
:root[data-theme="light"]{
  --bg:#F3F5FA;
  --bg-glow-1:rgba(91,124,255,.20);
  --bg-glow-2:rgba(167,139,250,.18);
  --bg-glow-3:rgba(45,226,197,.12);
  --surface:linear-gradient(180deg,rgba(255,255,255,.96),rgba(255,255,255,.88));
  --surface-flat:#FFFFFF;
  --surface-2:rgba(15,23,42,.045);
  --line:rgba(15,23,42,.10);
  --line-2:rgba(15,23,42,.18);
  --text:#0B1220;
  --muted:#55617A;
  --faint:#7C879B;
  --text-dim:rgba(11,18,32,.50);
  --bg-blur:rgba(243,245,250,.78);
  --accent:#3D5CE5;
  --accent-2:#7C5CE6;
  --grad:linear-gradient(135deg,#4361EE 0%,#6C4BE8 55%,#8B4BE8 100%);
  --ok:#0F9D6B;
  --ok-bg:rgba(15,157,107,.09);
  --ok-line:rgba(15,157,107,.30);
  --bad:#D93A52;
  --bad-bg:rgba(217,58,82,.08);
  --bad-line:rgba(217,58,82,.28);
  --ring:rgba(67,97,238,.40);
  --shadow:0 30px 60px -34px rgba(15,23,42,.35);
  --shadow-sm:0 10px 24px -16px rgba(15,23,42,.30);
  color-scheme:light;
}

*,*::before,*::after{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{
  margin:0;
  min-height:100dvh;
  background:var(--bg);
  color:var(--text);
  font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC","Hiragino Sans GB",system-ui,-apple-system,"Segoe UI",sans-serif;
  font-size:16px;
  line-height:1.6;
  -webkit-text-size-adjust:100%;
  text-rendering:optimizeLegibility;
  overflow-x:hidden;
}
body.is-booting{overflow:hidden;height:100dvh}
h1,h2,h3,p,figure{margin:0}
button{font:inherit;color:inherit;border:0;background:none;cursor:pointer}
img{display:block;max-width:100%}
::selection{background:rgba(108,140,255,.35)}

/* 背景光晕：纯 radial-gradient，不做 filter:blur，滚动与动画都不掉帧 */
.aurora{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden}
.aurora span{position:absolute;border-radius:50%;will-change:transform;transform:translateZ(0)}
.aurora .g1{width:130vw;height:130vw;left:-46vw;top:-64vw;background:radial-gradient(circle,rgba(91,124,255,.34) 0%,rgba(91,124,255,0) 62%);animation:drift1 30s var(--ease) infinite alternate}
.aurora .g2{width:110vw;height:110vw;right:-42vw;top:-24vw;background:radial-gradient(circle,rgba(155,123,255,.30) 0%,rgba(155,123,255,0) 60%);animation:drift2 34s var(--ease) infinite alternate}
.aurora .g3{width:120vw;height:120vw;left:-24vw;bottom:-62vw;background:radial-gradient(circle,rgba(45,226,197,.16) 0%,rgba(45,226,197,0) 60%);animation:drift3 38s var(--ease) infinite alternate}
:root[data-theme="light"] .aurora span{opacity:.55}
@keyframes drift1{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(6vw,5vw,0) scale(1.08)}}
@keyframes drift2{from{transform:translate3d(0,0,0) scale(1.06)}to{transform:translate3d(-7vw,6vw,0) scale(1)}}
@keyframes drift3{from{transform:translate3d(0,0,0) scale(1)}to{transform:translate3d(5vw,-6vw,0) scale(1.1)}}

/* ---------------- 启动动画 ---------------- */
.splash{
  position:fixed;inset:0;z-index:100;
  display:grid;place-items:center;
  background:
    radial-gradient(70% 55% at 50% 30%,rgba(91,124,255,.22),transparent 70%),
    radial-gradient(60% 50% at 50% 85%,rgba(155,123,255,.16),transparent 72%),
    var(--bg);
  transition:opacity .38s var(--ease),transform .38s var(--ease),filter .38s var(--ease);
  will-change:opacity,transform;
}
.splash.is-done{opacity:0;transform:scale(1.055);filter:blur(9px);pointer-events:none}
.splash-inner{display:flex;flex-direction:column;align-items:center;gap:0;padding:0 24px;text-align:center}
.splash-mark{position:relative;width:84px;height:84px;display:grid;place-items:center;animation:markIn .62s var(--spring) both}
.splash-mark .tile{
  position:absolute;inset:0;border-radius:24px;
  background:var(--grad);
  box-shadow:0 22px 44px -22px rgba(108,140,255,.85),inset 0 1px 0 rgba(255,255,255,.35);
}
.splash-mark .ring{
  position:absolute;inset:-12px;border-radius:32px;
  background:conic-gradient(from 0deg,rgba(108,140,255,0) 0deg,rgba(108,140,255,.85) 120deg,rgba(167,139,250,.15) 240deg,rgba(108,140,255,0) 360deg);
  -webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 2px),#000 calc(100% - 1px));
  mask:radial-gradient(farthest-side,transparent calc(100% - 2px),#000 calc(100% - 1px));
  animation:spin 1.5s linear infinite;
}
.splash-mark svg{position:relative;width:40px;height:40px;color:#fff}
.splash-mark svg .draw{stroke-dasharray:60;stroke-dashoffset:60;animation:draw .78s var(--ease) .12s forwards}
@keyframes markIn{0%{opacity:0;transform:scale(.82) translateY(6px)}60%{opacity:1}100%{opacity:1;transform:scale(1) translateY(0)}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes spin{to{transform:rotate(360deg)}}
.splash-word{
  margin-top:26px;font-size:22px;font-weight:800;letter-spacing:.32em;text-indent:.32em;
  background:linear-gradient(180deg,var(--text),var(--text-dim));
  -webkit-background-clip:text;background-clip:text;color:transparent;
  animation:riseIn .6s var(--ease) .18s both;
}
.splash-sub{margin-top:8px;font-size:12px;letter-spacing:.22em;color:var(--faint);text-transform:uppercase;animation:riseIn .6s var(--ease) .26s both}
.splash-bar{position:relative;width:180px;height:4px;margin-top:30px;border-radius:99px;background:var(--surface-2);overflow:hidden;animation:riseIn .6s var(--ease) .34s both}
.splash-bar i{position:absolute;left:0;top:0;bottom:0;width:6%;border-radius:99px;background:var(--grad);box-shadow:0 0 14px rgba(108,140,255,.7);transition:width .34s var(--ease)}
.splash-phase{margin-top:12px;font-size:13px;color:var(--muted);letter-spacing:.04em;min-height:20px;animation:riseIn .6s var(--ease) .4s both}
@keyframes riseIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

/* ---------------- 壳层 ---------------- */
.app{position:relative;z-index:1;padding:0 16px 40px;padding-bottom:calc(40px + env(safe-area-inset-bottom));max-width:1140px;margin:0 auto}
.topbar{
  position:sticky;top:0;z-index:20;
  display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:14px 2px 12px;
  background:var(--bg-blur);
  -webkit-backdrop-filter:saturate(160%) blur(14px);
  backdrop-filter:saturate(160%) blur(14px);
  border-bottom:1px solid transparent;
}
.topbar.is-stuck{border-bottom-color:var(--line)}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.brand .logo{
  width:38px;height:38px;border-radius:12px;display:grid;place-items:center;flex:0 0 auto;
  background:var(--grad);
  box-shadow:0 10px 22px -12px rgba(108,140,255,.9),inset 0 1px 0 rgba(255,255,255,.4);
}
.brand .logo svg{width:20px;height:20px;color:#fff}
.brand b{display:block;font-size:16px;font-weight:800;letter-spacing:.01em;line-height:1.25}
.brand small{display:block;font-size:11px;color:var(--faint);letter-spacing:.14em;text-transform:uppercase;line-height:1.3}
.topbar-right{display:flex;align-items:center;gap:8px}
.pill{
  display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 11px;border-radius:99px;
  font-size:12px;font-weight:700;color:var(--muted);
  background:var(--surface-flat);border:1px solid var(--line);
}
.pill .dot{width:6px;height:6px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.55)}70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.icon-btn{
  width:36px;height:36px;border-radius:11px;display:grid;place-items:center;
  background:var(--surface-flat);border:1px solid var(--line);color:var(--muted);
  transition:transform .22s var(--spring),color .2s,border-color .2s,background .2s;
}
.icon-btn:hover{color:var(--text);border-color:var(--line-2)}
.icon-btn:active{transform:scale(.92)}
.icon-btn svg{width:17px;height:17px}
.icon-btn .moon{display:none}
:root[data-theme="light"] .icon-btn .sun{display:none}
:root[data-theme="light"] .icon-btn .moon{display:block}

/* 入场：等启动动画退场后再依次浮入 */
.stagger{opacity:0}
body.is-ready .app>.topbar{animation:riseIn .55s var(--ease) both}
body.is-ready .stagger{animation:riseIn .62s var(--ease) both}
body.is-ready .stagger.d1{animation-delay:.05s}
body.is-ready .stagger.d2{animation-delay:.12s}
body.is-ready .stagger.d3{animation-delay:.19s}
body.is-ready .stagger.d4{animation-delay:.26s}

/* ---------------- 卡片 ---------------- */
.card{
  position:relative;
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:var(--r-lg);
  box-shadow:var(--shadow);
  -webkit-backdrop-filter:blur(16px) saturate(150%);
  backdrop-filter:blur(16px) saturate(150%);
}
.card::before{
  content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:linear-gradient(180deg,rgba(255,255,255,.10),transparent 42%);
  opacity:.7;
}
.grid{display:grid;gap:14px;margin-top:14px}

/* 二维码卡 */
.qr-card{display:flex;align-items:center;gap:14px;padding:14px;overflow:hidden}
.qr-frame{
  position:relative;flex:0 0 auto;width:104px;height:104px;border-radius:16px;padding:8px;
  background:#fff;box-shadow:var(--shadow-sm),0 0 0 1px rgba(255,255,255,.10);
}
.qr-frame img{width:100%;height:100%;border-radius:9px;object-fit:contain}
.qr-meta{min-width:0}
.qr-title{font-size:15.5px;font-weight:800;letter-spacing:.01em}
.qr-desc{margin-top:3px;font-size:13px;color:var(--muted);line-height:1.5}
.qr-addr{
  display:inline-flex;align-items:center;gap:6px;margin-top:9px;max-width:100%;
  padding:5px 10px;border-radius:9px;background:var(--surface-2);border:1px solid var(--line);
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.qr-addr svg{width:13px;height:13px;flex:0 0 auto;opacity:.8}

/* 工作区 */
.work-card{padding:12px;overflow:hidden}
.tabs{
  position:relative;display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:4px;
  border-radius:14px;background:var(--surface-2);
}
.tabs .glider{
  position:absolute;top:4px;bottom:4px;left:4px;width:calc((100% - 16px)/3);
  border-radius:11px;background:var(--surface-flat);
  border:1px solid var(--line-2);
  box-shadow:0 8px 18px -12px rgba(0,0,0,.65);
  transform:translateX(calc(var(--i,0) * (100% + 4px)));
  transition:transform .42s var(--spring);
  will-change:transform;
}
:root[data-theme="light"] .tabs .glider{background:#fff;box-shadow:0 6px 16px -10px rgba(15,23,42,.35)}
.tab{
  position:relative;z-index:1;display:flex;align-items:center;justify-content:center;gap:7px;
  height:44px;border-radius:11px;font-size:14.5px;font-weight:700;color:var(--muted);
  transition:color .24s var(--ease),transform .24s var(--spring);
}
.tab svg{width:16px;height:16px;opacity:.9}
.tab[aria-selected="true"]{color:var(--text)}
.tab:active{transform:scale(.96)}
.tab:focus-visible{outline:2px solid var(--ring);outline-offset:2px}

.panels{padding:16px 4px 4px}
.panel{display:none}
.panel.is-active{display:block;animation:panelIn .36s var(--ease) both}
@keyframes panelIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.field-label{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:9px}
.field-label span{font-size:13.5px;font-weight:700;color:var(--muted);letter-spacing:.02em}
.counter{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
.counter.is-warn{color:#FBBF24}

textarea{
  display:block;width:100%;min-height:150px;padding:14px 15px;resize:vertical;
  color:var(--text);background:var(--surface-2);
  border:1px solid var(--line);border-radius:var(--r-md);
  font-family:inherit;font-size:16.5px;line-height:1.7;
  transition:border-color .2s,box-shadow .2s,background .2s;
}
textarea::placeholder{color:var(--faint)}
textarea:focus{outline:none;border-color:var(--ring);box-shadow:0 0 0 3px var(--ring),inset 0 0 0 1px var(--ring);background:transparent}

/* 拖放区 */
.drop{
  position:relative;display:flex;align-items:center;gap:13px;
  padding:16px 15px;border-radius:var(--r-md);cursor:pointer;
  background:var(--surface-2);
  border:1.5px dashed var(--line-2);
  transition:border-color .22s,background .22s,transform .22s var(--spring);
}
.drop:hover{border-color:var(--ring);background:var(--surface-flat)}
.drop:active{transform:scale(.995)}
.drop.is-drag{border-color:var(--accent);background:rgba(108,140,255,.10);transform:scale(1.01)}
.drop:focus-within{outline:2px solid var(--ring);outline-offset:2px}
.drop .ico{
  width:42px;height:42px;flex:0 0 auto;border-radius:13px;display:grid;place-items:center;
  background:var(--grad);color:#fff;box-shadow:0 10px 20px -12px rgba(108,140,255,.9);
}
.drop .ico svg{width:19px;height:19px}
.drop .txt{min-width:0;flex:1}
.drop .txt b{display:block;font-size:15px;font-weight:800}
.drop .txt small{display:block;margin-top:2px;font-size:12.5px;color:var(--muted);line-height:1.45}
.drop .chev{color:var(--faint);flex:0 0 auto}
.drop .chev svg{width:16px;height:16px}
.drop input[type="file"]{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}

/* 已选文件条 */
.picked{display:none;align-items:center;gap:10px;margin-top:10px;padding:10px 12px;border-radius:var(--r-sm);background:var(--surface-2);border:1px solid var(--line)}
.picked.is-on{display:flex;animation:panelIn .3s var(--ease) both}
.picked .thumb{width:34px;height:34px;border-radius:9px;object-fit:cover;background:var(--surface-flat);flex:0 0 auto;border:1px solid var(--line)}
.picked .nm{min-width:0;flex:1;font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.picked .sz{font-size:11.5px;color:var(--faint);flex:0 0 auto;font-variant-numeric:tabular-nums}
.picked .x{width:26px;height:26px;border-radius:8px;display:grid;place-items:center;color:var(--faint);flex:0 0 auto}
.picked .x:hover{color:var(--text);background:var(--surface-flat)}
.picked .x svg{width:13px;height:13px}

.hint{margin-top:10px;font-size:12.5px;line-height:1.6;color:var(--muted)}
.hint.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:11px}
.hint.chips b{font-weight:700;font-size:11.5px;color:var(--muted);padding:3px 8px;border-radius:7px;background:var(--surface-2);border:1px solid var(--line)}

/* 进度条（上传） */
.bar{display:none;height:3px;margin-top:12px;border-radius:99px;background:var(--surface-2);overflow:hidden}
.bar.is-on{display:block}
.bar i{display:block;height:100%;width:0;border-radius:99px;background:var(--grad);transition:width .2s linear}

/* 按钮 */
.btn{
  position:relative;display:inline-flex;align-items:center;justify-content:center;gap:9px;
  width:100%;height:54px;margin-top:16px;border-radius:14px;
  font-size:16.5px;font-weight:800;letter-spacing:.02em;
  transition:transform .22s var(--spring),box-shadow .24s,opacity .2s,filter .2s;
  overflow:hidden;isolation:isolate;
}
.btn svg{width:18px;height:18px}
.btn-primary{color:#fff;background:var(--grad);box-shadow:0 16px 34px -18px rgba(108,140,255,.95),inset 0 1px 0 rgba(255,255,255,.28)}
.btn-primary::after{
  content:"";position:absolute;inset:0;z-index:-1;
  background:linear-gradient(100deg,transparent 20%,rgba(255,255,255,.30) 48%,transparent 72%);
  transform:translateX(-120%);transition:transform .7s var(--ease);
}
.btn-primary:hover::after{transform:translateX(120%)}
.btn-primary:hover{box-shadow:0 20px 40px -18px rgba(108,140,255,1)}
.btn-primary:active{transform:scale(.978)}
.btn-ghost{color:var(--text);background:var(--surface-2);border:1px solid var(--line-2);box-shadow:none}
.btn-ghost:hover{background:var(--surface-flat);border-color:var(--ring)}
.btn-ghost:active{transform:scale(.978)}
.btn[disabled]{cursor:not-allowed;filter:saturate(.5) brightness(.85);opacity:.75;transform:none}
.btn[disabled]::after{display:none}
.btn .spin{
  width:16px;height:16px;border-radius:50%;flex:0 0 auto;
  border:2px solid rgba(255,255,255,.35);border-top-color:#fff;
  animation:spin .7s linear infinite;
}
.btn:focus-visible{outline:2px solid var(--ring);outline-offset:3px}

/* 预览区 */
.preview{display:none;margin-top:14px}
.preview.is-on{display:block;animation:panelIn .34s var(--ease) both}
.preview-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.preview-title{font-size:13px;font-weight:700;color:var(--muted);min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.preview-pages{display:flex;gap:9px;overflow-x:auto;padding:2px 2px 10px;-webkit-overflow-scrolling:touch;scroll-snap-type:x proximity}
.preview-pages::-webkit-scrollbar{height:5px}
.preview-pages::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:9px}
.preview-pages img{flex:0 0 auto;width:62%;max-width:250px;height:auto;border-radius:11px;background:#fff;border:1px solid var(--line-2);scroll-snap-align:start;box-shadow:var(--shadow-sm)}
.preview-pages .ph{
  flex:0 0 auto;width:62%;max-width:250px;aspect-ratio:210/297;border-radius:11px;
  background:linear-gradient(100deg,var(--surface-2) 20%,rgba(255,255,255,.10) 40%,var(--surface-2) 60%);
  background-size:220% 100%;animation:shimmer 1.3s linear infinite;border:1px solid var(--line);
}
@keyframes shimmer{from{background-position:120% 0}to{background-position:-120% 0}}

/* 状态卡 */
.status{
  display:none;margin-top:14px;padding:14px;border-radius:var(--r-md);
  border:1px solid var(--line);
}
.status.is-on{display:flex;gap:11px;align-items:flex-start;animation:statusIn .44s var(--spring) both}
@keyframes statusIn{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}
.status.ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok-line)}
.status.bad{color:var(--bad);background:var(--bad-bg);border-color:var(--bad-line)}
.status-ico{width:26px;height:26px;flex:0 0 auto;border-radius:50%;display:grid;place-items:center;margin-top:1px}
.status.ok .status-ico{background:rgba(52,211,153,.16)}
.status.bad .status-ico{background:rgba(251,113,133,.16)}
.status-ico svg{width:15px;height:15px}
.status-body{flex:1;min-width:0;color:var(--text)}
.status-title{font-size:15px;font-weight:800;line-height:1.45}
.status.ok .status-title{color:var(--ok)}
.status.bad .status-title{color:var(--bad)}
.status-help{margin-top:6px;font-size:13px;line-height:1.65;color:var(--muted)}
.status-help p{margin:0}
.status-help ul{margin:6px 0 0;padding-left:1.15em}
.status-help li{margin:5px 0}
.status-help strong{font-weight:800;color:var(--text)}
.status-x{width:26px;height:26px;flex:0 0 auto;border-radius:8px;display:grid;place-items:center;color:var(--faint)}
.status-x:hover{color:var(--text);background:var(--surface-2)}
.status-x svg{width:12px;height:12px}

/* 打印中状态 */
.printing{display:none;align-items:center;gap:9px;margin-top:12px;padding:10px 13px;border-radius:var(--r-sm);background:var(--surface-2);border:1px solid var(--line);font-size:13px;color:var(--muted)}
.printing.is-on{display:flex;animation:panelIn .3s var(--ease) both}
.dots{display:inline-flex;gap:4px}
.dots i{width:5px;height:5px;border-radius:50%;background:var(--accent);animation:bounce 1.1s ease-in-out infinite}
.dots i:nth-child(2){animation-delay:.15s}
.dots i:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:.45}30%{transform:translateY(-4px);opacity:1}}

/* Toast */
.toasts{position:fixed;left:50%;bottom:calc(22px + env(safe-area-inset-bottom));z-index:90;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none;width:calc(100% - 32px);max-width:400px}
.toast{
  display:flex;align-items:center;gap:9px;max-width:100%;padding:11px 15px;border-radius:13px;
  background:rgba(20,26,40,.94);color:#EDF2FB;font-size:14px;font-weight:600;
  border:1px solid rgba(255,255,255,.14);box-shadow:0 18px 40px -20px rgba(0,0,0,.9);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  animation:toastIn .42s var(--spring) both;
}
:root[data-theme="light"] .toast{background:rgba(255,255,255,.97);color:#0B1220;border-color:rgba(15,23,42,.12);box-shadow:0 18px 40px -22px rgba(15,23,42,.4)}
.toast.is-out{animation:toastOut .3s var(--ease) both}
.toast svg{width:15px;height:15px;flex:0 0 auto;color:#FBBF24}
@keyframes toastIn{from{opacity:0;transform:translateY(14px) scale(.96)}to{opacity:1;transform:none}}
@keyframes toastOut{to{opacity:0;transform:translateY(8px) scale(.97)}}

/* 页脚 */
.foot{margin-top:22px;text-align:center;font-size:12px;color:var(--faint);line-height:1.9}
.foot b{font-weight:700;color:var(--muted)}

/* ---------------- 响应式 ---------------- */
@media (min-width:720px){
  .app{padding:0 24px 56px}
  .grid{grid-template-columns:300px minmax(0,1fr);gap:18px;margin-top:18px}
  .qr-card{flex-direction:column;align-items:stretch;padding:20px;gap:16px}
  .qr-frame{width:100%;height:auto;min-height:240px;aspect-ratio:1;padding:10px}
  .qr-title{font-size:16px}
  .qr-desc{font-size:13.5px;margin-top:6px}
  .qr-addr{font-size:12.5px}
  .work-card{padding:14px}
  .panels{padding:20px 6px 6px}
  textarea{min-height:210px}
  .status{margin-top:18px;padding:16px}
  .drop{padding:20px 18px}
}
@media (min-width:1024px){
  .grid{grid-template-columns:340px minmax(0,1fr);gap:22px}
  .qr-card{position:sticky;top:76px}
  .app{padding:0 32px 64px}
  .brand b{font-size:17px}
  textarea{min-height:240px;font-size:17px}
}
@media (max-width:380px){
  .qr-frame{width:88px;height:88px;padding:6px}
  .qr-desc{display:none}
  .brand small{display:none}
  .tab span{display:none}
  .tab{gap:0}
  .btn{font-size:16px}
}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}
  .stagger{opacity:1}
  .splash.is-done{filter:none;transform:none;transition:opacity .18s linear}
}
</style>
</head>
<body class="is-booting">

<!-- 启动动画 -->
<div class="splash" id="splash" role="status" aria-live="polite" aria-label="正在加载打印页面">
  <div class="splash-inner">
    <div class="splash-mark">
      <span class="tile"></span>
      <span class="ring"></span>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path class="draw" d="M7 8V4h10v4"/>
        <path class="draw" d="M7 17H5.5A1.5 1.5 0 0 1 4 15.5v-5A1.5 1.5 0 0 1 5.5 9h13A1.5 1.5 0 0 1 20 10.5v5a1.5 1.5 0 0 1-1.5 1.5H17"/>
        <path class="draw" d="M7 14h10v6H7z"/>
      </svg>
    </div>
    <div class="splash-word">家庭打印</div>
    <div class="splash-sub">HOME PRINT · LAN</div>
    <div class="splash-bar"><i id="splashBar"></i></div>
    <div class="splash-phase" id="splashPhase">正在连接打印服务…</div>
  </div>
</div>

<div class="aurora" aria-hidden="true"><span class="g1"></span><span class="g2"></span><span class="g3"></span></div>

<div class="app">
  <header class="topbar" id="topbar">
    <div class="brand">
      <span class="logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M7 8V4h10v4"/><path d="M7 17H5.5A1.5 1.5 0 0 1 4 15.5v-5A1.5 1.5 0 0 1 5.5 9h13A1.5 1.5 0 0 1 20 10.5v5a1.5 1.5 0 0 1-1.5 1.5H17"/><path d="M7 14h10v6H7z"/>
        </svg>
      </span>
      <div>
        <b>家庭打印</b>
        <small>Home Print</small>
      </div>
    </div>
    <div class="topbar-right">
      <span class="pill"><i class="dot"></i>局域网直连</span>
      <button class="icon-btn" id="themeToggle" type="button" aria-label="切换深浅色">
        <svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        <svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.6 6.6 0 0 0 9.8 9.8z"/></svg>
      </button>
    </div>
  </header>

  <div class="grid">
    <!-- 扫码卡 -->
    <aside class="card qr-card stagger d1">
      <div class="qr-frame">
        <img id="qrImg" src="{{ qr_url }}" alt="内网访问二维码" width="360" height="360" fetchpriority="high" decoding="async">
      </div>
      <div class="qr-meta">
        <p class="qr-title">手机扫码打开</p>
        <p class="qr-desc">手机和这台电脑连同一个 WiFi，扫一下就能开始打印。</p>
        <div class="qr-addr">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7L12 19"/></svg>
          <span>{{ lan_url }}</span>
        </div>
      </div>
    </aside>

    <!-- 工作区 -->
    <section class="card work-card stagger d2">
      <div class="tabs" role="tablist" aria-label="选择打印方式" id="tabs" style="--i:0">
        <span class="glider" aria-hidden="true"></span>
        <button class="tab" id="tab-text" type="button" role="tab" aria-selected="true" aria-controls="panel-text" data-tab="text">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h13"/></svg><span>文字</span>
        </button>
        <button class="tab" id="tab-image" type="button" role="tab" aria-selected="false" aria-controls="panel-image" data-tab="image" tabindex="-1">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M21 16l-5-5-6.5 6.5"/></svg><span>图片</span>
        </button>
        <button class="tab" id="tab-file" type="button" role="tab" aria-selected="false" aria-controls="panel-file" data-tab="file" tabindex="-1">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg><span>文件</span>
        </button>
      </div>

      <div class="panels">
        <!-- 文字 -->
        <div class="panel is-active" id="panel-text" role="tabpanel" aria-labelledby="tab-text">
          <div class="field-label">
            <span>要打印的内容</span>
            <span class="counter" id="textCounter">0 / {{ max_text_chars }}</span>
          </div>
          <textarea id="textInput" maxlength="{{ max_text_chars }}" autocomplete="off" placeholder="在这里输入或粘贴要打印的文字，例如菜谱、作业、地址……"></textarea>
          <button class="btn btn-primary" id="textButton" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 8V4h10v4"/><path d="M7 17H5.5A1.5 1.5 0 0 1 4 15.5v-5A1.5 1.5 0 0 1 5.5 9h13A1.5 1.5 0 0 1 20 10.5v5a1.5 1.5 0 0 1-1.5 1.5H17"/><path d="M7 14h10v6H7z"/></svg>
            <span class="lbl">打印文字</span>
          </button>
        </div>

        <!-- 图片 -->
        <div class="panel" id="panel-image" role="tabpanel" aria-labelledby="tab-image" hidden>
          <label class="drop" for="imageInput" id="imageDrop">
            <span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M21 16l-5-5-6.5 6.5"/></svg></span>
            <span class="txt"><b>选择相册里的照片</b><small>支持 JPG / PNG / WEBP，最大 {{ max_image_mb }} MB</small></span>
            <span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg></span>
            <input id="imageInput" type="file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp">
          </label>
          <div class="picked" id="imagePicked">
            <img class="thumb" id="imageThumb" alt="">
            <span class="nm" id="imageName"></span>
            <span class="sz" id="imageSize"></span>
            <button class="x" id="imageClear" type="button" aria-label="移除图片"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
          </div>
          <div class="bar" id="imageBar"><i></i></div>
          <p class="hint">打印时会自动按 A4 纸排版，整张照片缩放到一页内。</p>
          <button class="btn btn-primary" id="imageButton" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 8V4h10v4"/><path d="M7 17H5.5A1.5 1.5 0 0 1 4 15.5v-5A1.5 1.5 0 0 1 5.5 9h13A1.5 1.5 0 0 1 20 10.5v5a1.5 1.5 0 0 1-1.5 1.5H17"/><path d="M7 14h10v6H7z"/></svg>
            <span class="lbl">打印图片</span>
          </button>
        </div>

        <!-- 文件 -->
        <div class="panel" id="panel-file" role="tabpanel" aria-labelledby="tab-file" hidden>
          <label class="drop" for="fileInput" id="fileDrop">
            <span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg></span>
            <span class="txt"><b>选择文件或微信聊天文件</b><small>最大 {{ max_file_mb }} MB，先预览再打印</small></span>
            <span class="chev"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 5l7 7-7 7"/></svg></span>
            <input id="fileInput" type="file">
          </label>
          <div class="picked" id="filePicked">
            <span class="thumb" style="display:grid;place-items:center;color:var(--muted)"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg></span>
            <span class="nm" id="fileName"></span>
            <span class="sz" id="fileSize"></span>
            <button class="x" id="fileClear" type="button" aria-label="移除文件"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
          </div>
          <div class="hint chips">
            <b>PDF</b><b>Word</b><b>Excel</b><b>PPT</b><b>TXT</b><b>HTML</b><b>图片</b>
          </div>
          <p class="hint">微信里的文件，请在文件选择器中选「聊天文件」；如果没看到，先把文件保存到手机。</p>
          <div class="bar" id="fileBar"><i></i></div>
          <button class="btn btn-ghost" id="previewFileButton" type="button">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.8"/></svg>
            <span class="lbl">预览文件</span>
          </button>
          <div class="preview" id="filePreviewWrap">
            <div class="preview-head"><span class="preview-title" id="filePreviewTitle"></span></div>
            <div class="preview-pages" id="filePreviewPages"></div>
            <button class="btn btn-primary" id="confirmFileButton" type="button">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>
              <span class="lbl">确认打印这份文件</span>
            </button>
          </div>
        </div>

        <div class="printing" id="printingBar">
          <span class="dots"><i></i><i></i><i></i></span>
          <span>已进入打印队列，正在打印…</span>
        </div>

        <div class="status" id="statusBox" role="status" aria-live="polite">
          <span class="status-ico" id="statusIco"></span>
          <div class="status-body">
            <p class="status-title" id="statusTitle"></p>
            <div class="status-help" id="statusHelp"></div>
          </div>
          <button class="status-x" id="statusClose" type="button" aria-label="关闭提示"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
        </div>
      </div>
    </section>
  </div>

  <footer class="foot stagger d3">
    <p>打印任务按顺序排队 · 请保持电脑与打印机开机</p>
    <p>密码忘了？问家里帮您设置这台电脑的人</p>
  </footer>
</div>

<div class="toasts" id="toasts" aria-live="assertive"></div>

<script>
(function () {
  "use strict";
  var MAX_IMAGE_BYTES = {{ max_image_bytes }};
  var MAX_IMAGE_MB = Math.round(MAX_IMAGE_BYTES / (1024 * 1024));
  var MAX_FILE_BYTES = {{ max_file_bytes }};
  var MAX_FILE_MB = Math.round(MAX_FILE_BYTES / (1024 * 1024));
  var REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  function $(id) { return document.getElementById(id); }

  /* ---------------- 启动动画：真实就绪驱动，最短 680ms / 最长 1500ms ---------------- */
  var boot = (function () {
    var splash = $("splash"), bar = $("splashBar"), phase = $("splashPhase");
    if (!splash) { return { done: function () {} }; }
    var MIN_SHOW = REDUCED ? 220 : 680;
    var MAX_WAIT = REDUCED ? 600 : 1500;
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    var finished = false, ready = false, tick = null;

    function setP(p, text) {
      if (bar) { bar.style.width = p + "%"; }
      if (text && phase) { phase.textContent = text; }
    }
    function now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

    // 0→78% 的推进感：不让进度条卡死在原地
    var p = 6;
    setP(p);
    tick = window.setInterval(function () {
      if (ready) { return; }
      var elapsed = now() - t0;
      var cap = 78 * (1 - Math.exp(-elapsed / 520));
      if (cap > p) { p = cap; setP(Math.min(78, p)); }
    }, 90);

    function finish() {
      if (finished) { return; }
      finished = true;
      window.clearInterval(tick);
      setP(100, "准备就绪");
      window.setTimeout(function () {
        splash.classList.add("is-done");
        document.body.classList.remove("is-booting");
        document.body.classList.add("is-ready");
        window.setTimeout(function () {
          if (splash.parentNode) { splash.parentNode.removeChild(splash); }
        }, REDUCED ? 200 : 420);
      }, REDUCED ? 40 : 170);
    }

    function markReady(text) {
      ready = true;
      setP(100, text || "准备就绪");
      var elapsed = now() - t0;
      window.setTimeout(finish, Math.max(0, MIN_SHOW - elapsed));
    }

    if (document.readyState === "complete") {
      markReady("准备就绪");
    } else {
      document.addEventListener("DOMContentLoaded", function () { setP(52, "正在加载界面…"); });
      window.addEventListener("load", function () { setP(90, "正在准备二维码…"); markReady("准备就绪"); });
      // 兜底：图片迟迟不返回也不能一直转圈
      window.setTimeout(function () { if (!finished) { markReady("已就绪，可直接使用"); } }, MAX_WAIT);
    }
    return { done: finish };
  })();

  /* ---------------- Toast ---------------- */
  var toasts = $("toasts");
  function toast(msg) {
    if (!toasts) { return; }
    var el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.01"/></svg>';
    el.appendChild(document.createTextNode(msg));
    toasts.appendChild(el);
    window.setTimeout(function () {
      el.classList.add("is-out");
      window.setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, 320);
    }, 2600);
  }

  /* ---------------- 主题 ---------------- */
  var themeBtn = $("themeToggle");
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  function syncThemeColor() {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) { meta.setAttribute("content", currentTheme() === "light" ? "#F3F5FA" : "#060911"); }
  }
  syncThemeColor();
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = currentTheme() === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("fp-theme", next); } catch (e) {}
      syncThemeColor();
    });
  }

  /* ---------------- 顶栏滚动阴影 ---------------- */
  var topbar = $("topbar");
  var onScroll = function () {
    if (!topbar) { return; }
    if (window.scrollY > 4) { topbar.classList.add("is-stuck"); }
    else { topbar.classList.remove("is-stuck"); }
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  /* ---------------- Tabs ---------------- */
  var tabWrap = $("tabs");
  var tabs = [].slice.call(document.querySelectorAll(".tab"));
  var panels = { text: $("panel-text"), image: $("panel-image"), file: $("panel-file") };

  function selectTab(name, focus) {
    var idx = 0;
    tabs.forEach(function (t, i) {
      var on = t.getAttribute("data-tab") === name;
      if (on) { idx = i; }
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    Object.keys(panels).forEach(function (k) {
      var el = panels[k];
      var on = k === name;
      el.classList.toggle("is-active", on);
      if (on) { el.removeAttribute("hidden"); }
      else { el.setAttribute("hidden", ""); }
    });
    if (tabWrap) { tabWrap.style.setProperty("--i", String(idx)); }
    if (focus) { tabs[idx].focus(); }
  }
  tabs.forEach(function (t) {
    t.addEventListener("click", function () { selectTab(t.getAttribute("data-tab")); });
    t.addEventListener("keydown", function (e) {
      var idx = tabs.indexOf(t), next = null;
      if (e.key === "ArrowRight") { next = (idx + 1) % tabs.length; }
      if (e.key === "ArrowLeft") { next = (idx - 1 + tabs.length) % tabs.length; }
      if (e.key === "Home") { next = 0; }
      if (e.key === "End") { next = tabs.length - 1; }
      if (next !== null) {
        e.preventDefault();
        selectTab(tabs[next].getAttribute("data-tab"), true);
      }
    });
  });

  /* ---------------- 通用 UI 状态 ---------------- */
  var statusBox = $("statusBox"), statusTitle = $("statusTitle"), statusHelp = $("statusHelp");
  var statusIco = $("statusIco"), statusClose = $("statusClose");
  var printingBar = $("printingBar");

  var ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
  var ICON_BAD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v5M12 16.5v.01"/><circle cx="12" cy="12" r="9"/></svg>';

  var PRINT_REASON = {
    paper_out: "打印机缺纸",
    paper_jam: "打印机卡纸",
    offline: "打印机离线或连接松动",
    door_open: "打印机盖没有关好",
    no_toner: "打印机缺墨或墨粉",
    paused: "打印机被暂停",
    no_printer: "电脑没有设置默认打印机",
    output_full: "打印机出纸口满了",
    not_available: "打印机当前不可用",
    server_offline: "网络打印机离线",
    paper_problem: "打印机纸张故障",
    out_of_memory: "打印机内存不足",
    user_intervention: "打印机需要人工处理",
    page_punt: "打印机无法打印当前页",
    printer_error: "打印机或驱动报错"
  };
  var REASON_EXTRA = {
    no_printer: ["默认打印机：打开 Windows 设置 → 蓝牙和其他设备 → 打印机和扫描仪，点你的打印机 → 设为默认值。"],
    door_open: ["打印机盖：打开再重新关紧墨盒/硒鼓盖和纸盒，听到卡扣声后再试。"],
    no_toner: ["墨粉：检查墨盒/硒鼓是否装好，缺墨请更换或联系家人。"],
    paused: ["暂停状态：在 Windows 打印机队列里点「继续」，或按打印机上的继续键。"],
    output_full: ["出纸口：把已经打印出来的纸拿走，再重新提交。"]
  };

  function hideStatus() {
    statusBox.classList.remove("is-on", "ok", "bad");
  }
  function showStatus(ok, reason) {
    var known = reason ? PRINT_REASON[reason] : "";
    statusIco.innerHTML = ok ? ICON_OK : ICON_BAD;
    statusTitle.textContent = ok ? "已提交，正在排队打印" : (known ? "打印失败：" + known : "打印失败");
    statusHelp.innerHTML = "";
    if (!ok) {
      var intro = document.createElement("p");
      intro.textContent = known
        ? "请按下面的办法处理："
        : "可能是缺纸、卡纸，或者打印机离线/连接松动。请按下面的办法处理：";
      statusHelp.appendChild(intro);

      var list = document.createElement("ul");
      var items = [
        ["缺纸：", "打开纸盒放入 A4 纸，把纸盒推回，按打印机上的「继续/开始」键，然后回到本页重新打印。"],
        ["卡纸：", "打开打印机盖或后盖，顺着纸张走纸方向轻轻拉出，不要硬拽；关好盖，按「继续」键再试。"],
        ["离线/线松：", "检查打印机电源、USB 线或 WiFi；到 Windows 设置里确认默认打印机在线，再重新提交。"],
        ["其他：", "看电脑上的打印程序窗口或本程序控制台，按提示处理后再试一次。"]
      ];
      (REASON_EXTRA[reason] || []).forEach(function (extra) {
        var parts = extra.split("：");
        items.unshift([parts[0] + "：", parts.slice(1).join("：")]);
      });
      items.forEach(function (item) {
        var li = document.createElement("li");
        var strong = document.createElement("strong");
        strong.textContent = item[0];
        li.appendChild(strong);
        li.appendChild(document.createTextNode(item[1]));
        list.appendChild(li);
      });
      statusHelp.appendChild(list);
    }
    statusBox.classList.remove("ok", "bad");
    statusBox.classList.add("is-on", ok ? "ok" : "bad");
    if (!ok) { statusBox.scrollIntoView({ block: "nearest", behavior: REDUCED ? "auto" : "smooth" }); }
  }
  if (statusClose) { statusClose.addEventListener("click", hideStatus); }

  /* ---------------- 按钮忙碌态 ---------------- */
  function busy(btn, on, label) {
    if (!btn) { return; }
    var textEl = btn.querySelector(".lbl");
    if (on) {
      if (!btn.getAttribute("data-label")) {
        btn.setAttribute("data-label", textEl ? textEl.textContent : btn.textContent);
      }
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      if (textEl) { textEl.textContent = label || "正在提交…"; }
      if (!btn.querySelector(".spin")) {
        var sp = document.createElement("span");
        sp.className = "spin";
        btn.insertBefore(sp, btn.firstChild);
      }
    } else {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      var sp2 = btn.querySelector(".spin");
      if (sp2) { sp2.parentNode.removeChild(sp2); }
      var lab = btn.getAttribute("data-label");
      if (lab && textEl) { textEl.textContent = lab; }
      btn.removeAttribute("data-label");
    }
  }

  function handleUnauthorized(status) {
    if (status === 401) {
      window.location.href = "/";
      return true;
    }
    return false;
  }

  /* ---------------- 上传进度（XHR 才有真实进度） ---------------- */
  function barOf(id) {
    var el = $(id);
    return {
      show: function () { if (el) { el.classList.add("is-on"); this.set(0); } },
      set: function (p) { var i = el && el.firstElementChild; if (i) { i.style.width = Math.max(0, Math.min(100, p)) + "%"; } },
      hide: function () {
        if (!el) { return; }
        var self = this;
        window.setTimeout(function () { el.classList.remove("is-on"); self.set(0); }, 380);
      }
    };
  }

  function post(url, body, opts, cb) {
    var xhr = new XMLHttpRequest();
    var bar = opts && opts.bar;
    xhr.open("POST", url, true);
    if (opts && opts.json) { xhr.setRequestHeader("Content-Type", "application/json"); }
    if (xhr.upload && bar) {
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) { bar.set(Math.round(e.loaded / e.total * 92)); }
      };
    }
    xhr.onload = function () {
      if (handleUnauthorized(xhr.status)) { return; }
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      cb({ ok: xhr.status >= 200 && xhr.status < 300 && !!data && data.ok === true, status: xhr.status, data: data || {} });
    };
    xhr.onerror = function () { cb({ ok: false, status: 0, data: {}, network: true }); };
    xhr.send(body);
  }

  function poll(taskId) {
    var tries = 0;
    if (printingBar) { printingBar.classList.add("is-on"); }
    var timer = window.setInterval(function () {
      tries += 1;
      if (tries > 3600) { window.clearInterval(timer); if (printingBar) { printingBar.classList.remove("is-on"); } return; }
      var xhr = new XMLHttpRequest();
      xhr.open("GET", "/status/" + encodeURIComponent(taskId), true);
      xhr.onload = function () {
        if (handleUnauthorized(xhr.status)) { window.clearInterval(timer); return; }
        if (xhr.status !== 200) { return; }
        var data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { return; }
        if (!data) { return; }
        if (data.status === "error") {
          window.clearInterval(timer);
          if (printingBar) { printingBar.classList.remove("is-on"); }
          showStatus(false, data.reason || "");
        } else if (data.status === "done" || data.status === "unknown") {
          window.clearInterval(timer);
          if (printingBar) { printingBar.classList.remove("is-on"); }
        }
      };
      xhr.send();
    }, 1200);
  }

  function humanSize(bytes) {
    if (!bytes && bytes !== 0) { return ""; }
    if (bytes < 1024) { return bytes + " B"; }
    if (bytes < 1024 * 1024) { return (bytes / 1024).toFixed(0) + " KB"; }
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  /* ---------------- 文字 ---------------- */
  var textInput = $("textInput"), textButton = $("textButton"), textCounter = $("textCounter");
  var MAX_TEXT = {{ max_text_chars }};
  function syncCounter() {
    if (!textCounter || !textInput) { return; }
    var n = textInput.value.length;
    textCounter.textContent = n + " / " + MAX_TEXT;
    textCounter.classList.toggle("is-warn", n > MAX_TEXT * 0.9);
  }
  if (textInput) {
    textInput.addEventListener("input", syncCounter);
    syncCounter();
  }
  if (textButton) {
    textButton.addEventListener("click", function () {
      var text = textInput.value;
      if (!text.trim()) {
        toast("请先输入要打印的文字");
        textInput.focus();
        return;
      }
      hideStatus();
      busy(textButton, true, "正在提交…");
      post("/print/text", JSON.stringify({ text: text }), { json: true }, function (res) {
        busy(textButton, false);
        if (res.ok) {
          showStatus(true);
          if (res.data.task_id) { poll(res.data.task_id); }
        } else if (res.network) {
          toast("网络连接失败，请重试");
        } else {
          showStatus(false);
        }
      });
    });
  }

  /* ---------------- 图片 ---------------- */
  var imageInput = $("imageInput"), imageButton = $("imageButton");
  var imagePicked = $("imagePicked"), imageName = $("imageName"), imageSize = $("imageSize");
  var imageThumb = $("imageThumb"), imageClear = $("imageClear"), imageDrop = $("imageDrop");
  var imageBar = barOf("imageBar");
  var imageUrl = null;

  function resetImage() {
    imageInput.value = "";
    if (imagePicked) { imagePicked.classList.remove("is-on"); }
    if (imageUrl) { URL.revokeObjectURL(imageUrl); imageUrl = null; }
  }
  if (imageInput) {
    imageInput.addEventListener("change", function () {
      var f = imageInput.files && imageInput.files[0];
      if (!f) { return; }
      if (imageName) { imageName.textContent = f.name; }
      if (imageSize) { imageSize.textContent = humanSize(f.size); }
      if (imageThumb && f.type && f.type.indexOf("image/") === 0) {
        if (imageUrl) { URL.revokeObjectURL(imageUrl); }
        imageUrl = URL.createObjectURL(f);
        imageThumb.src = imageUrl;
      }
      if (imagePicked) { imagePicked.classList.add("is-on"); }
    });
  }
  if (imageClear) {
    imageClear.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); resetImage(); });
  }
  if (imageDrop) {
    ["dragenter", "dragover"].forEach(function (ev) {
      imageDrop.addEventListener(ev, function (e) { e.preventDefault(); imageDrop.classList.add("is-drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      imageDrop.addEventListener(ev, function () { imageDrop.classList.remove("is-drag"); });
    });
  }
  if (imageButton) {
    imageButton.addEventListener("click", function () {
      var file = imageInput.files && imageInput.files[0];
      if (!file) { toast("请先选择手机相册里的图片"); return; }
      if (!/\.(jpe?g|png|webp)$/i.test(file.name)) { toast("只支持 jpg、png、webp 图片"); return; }
      if (file.size > MAX_IMAGE_BYTES) { toast("图片太大了，请换一张不超过 " + MAX_IMAGE_MB + "MB 的图片"); return; }
      var fd = new FormData();
      fd.append("image", file, file.name);
      hideStatus();
      busy(imageButton, true, "正在上传…");
      imageBar.show();
      post("/print/image", fd, { bar: imageBar }, function (res) {
        busy(imageButton, false);
        imageBar.hide();
        if (res.ok) {
          showStatus(true);
          resetImage();
          if (res.data.task_id) { poll(res.data.task_id); }
        } else if (res.network) {
          toast("网络连接失败，请重试");
        } else {
          showStatus(false);
        }
      });
    });
  }

  /* ---------------- 文件 ---------------- */
  var fileInput = $("fileInput"), previewFileButton = $("previewFileButton");
  var filePicked = $("filePicked"), fileName = $("fileName"), fileSize = $("fileSize"), fileClear = $("fileClear");
  var fileDrop = $("fileDrop");
  var fileBar = barOf("fileBar");
  var filePreviewWrap = $("filePreviewWrap"), filePreviewTitle = $("filePreviewTitle"), filePreviewPages = $("filePreviewPages");
  var confirmFileButton = $("confirmFileButton");
  var currentFileId = null;

  function clearFilePreview() {
    currentFileId = null;
    if (filePreviewWrap) { filePreviewWrap.classList.remove("is-on"); }
    if (filePreviewTitle) { filePreviewTitle.textContent = ""; }
    if (filePreviewPages) { filePreviewPages.innerHTML = ""; }
  }
  function resetFile() {
    if (fileInput) { fileInput.value = ""; }
    if (filePicked) { filePicked.classList.remove("is-on"); }
    clearFilePreview();
  }
  if (fileInput) {
    fileInput.addEventListener("change", function () {
      clearFilePreview();
      var f = fileInput.files && fileInput.files[0];
      if (!f) { return; }
      if (fileName) { fileName.textContent = f.name; }
      if (fileSize) { fileSize.textContent = humanSize(f.size); }
      if (filePicked) { filePicked.classList.add("is-on"); }
    });
  }
  if (fileClear) {
    fileClear.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); resetFile(); });
  }
  if (fileDrop) {
    ["dragenter", "dragover"].forEach(function (ev) {
      fileDrop.addEventListener(ev, function (e) { e.preventDefault(); fileDrop.classList.add("is-drag"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      fileDrop.addEventListener(ev, function () { fileDrop.classList.remove("is-drag"); });
    });
  }

  if (previewFileButton) {
    previewFileButton.addEventListener("click", function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) { toast("请先选择手机文件或微信聊天文件"); return; }
      if (file.size > MAX_FILE_BYTES) { toast("文件太大了，请上传不超过 " + MAX_FILE_MB + "MB 的文件"); return; }

      var fd = new FormData();
      fd.append("file", file, file.name);
      clearFilePreview();
      busy(previewFileButton, true, "正在准备预览…");
      fileBar.show();

      // 先铺骨架屏，等待期间页面不空
      filePreviewPages.innerHTML = '<div class="ph"></div><div class="ph"></div>';
      filePreviewTitle.textContent = "正在生成预览…";
      filePreviewWrap.classList.add("is-on");

      post("/print/file", fd, { bar: fileBar }, function (res) {
        busy(previewFileButton, false);
        fileBar.hide();
        if (res.network) { toast("网络连接失败，请重试"); clearFilePreview(); return; }
        if (!res.ok) {
          clearFilePreview();
          toast((res.data && res.data.message) || "文件处理失败，请换一个文件再试");
          return;
        }
        var data = res.data;
        currentFileId = data.file_id;
        var title = data.name + " · 共 " + data.pages + " 页";
        if (data.preview_count < data.pages) {
          title += "（仅预览前 " + data.preview_count + " 页）";
        }
        filePreviewTitle.textContent = title;
        filePreviewPages.innerHTML = "";
        (data.previews || []).forEach(function (src, index) {
          var img = document.createElement("img");
          img.src = src;
          img.alt = "第 " + (index + 1) + " 页预览";
          img.loading = "lazy";
          img.decoding = "async";
          filePreviewPages.appendChild(img);
        });
        filePreviewWrap.classList.add("is-on");
      });
    });
  }

  if (confirmFileButton) {
    confirmFileButton.addEventListener("click", function () {
      if (!currentFileId) { toast("请先选择文件并预览"); return; }
      busy(confirmFileButton, true, "正在提交打印…");
      post("/print/file/" + encodeURIComponent(currentFileId), null, {}, function (res) {
        busy(confirmFileButton, false);
        if (res.network) { toast("网络连接失败，请重试"); return; }
        if (res.ok) {
          showStatus(true);
          if (res.data.task_id) { poll(res.data.task_id); }
          resetFile();
        } else {
          toast((res.data && res.data.message) || "文件提交失败，请重新选择");
        }
      });
    });
  }
})();
</script>
</body>
</html>
"""


ERROR_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#060911">
<title>家庭打印 · 出了点问题</title>
<style>
  :root{
    --bg:#060911;--text:#EAF0FA;--muted:#98A5BA;--line:rgba(255,255,255,.10);
    --surface:linear-gradient(180deg,rgba(255,255,255,.07),rgba(255,255,255,.03));
    --grad:linear-gradient(135deg,#5B7CFF 0%,#8B6DFF 55%,#B06CFF 100%);
    --ease:cubic-bezier(.16,1,.3,1);--spring:cubic-bezier(.22,1.2,.36,1);
    color-scheme:dark;
  }
  @media (prefers-color-scheme: light){
    :root{
      --bg:#F3F5FA;--text:#0B1220;--muted:#55617A;--line:rgba(15,23,42,.10);
      --surface:linear-gradient(180deg,rgba(255,255,255,.98),rgba(255,255,255,.9));
      color-scheme:light;
    }
  }
  *,*::before,*::after{box-sizing:border-box}
  body{
    margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px 18px;
    background:radial-gradient(70% 55% at 50% 0%,rgba(91,124,255,.20),transparent 68%),var(--bg);
    color:var(--text);
    font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,-apple-system,sans-serif;
    font-size:16px;line-height:1.6;-webkit-text-size-adjust:100%;
  }
  .card{
    width:100%;max-width:420px;padding:30px 24px;border-radius:24px;text-align:center;
    background:var(--surface);border:1px solid var(--line);
    box-shadow:0 40px 90px -44px rgba(0,0,0,.9);
    animation:riseIn .6s var(--ease) both;
  }
  @keyframes riseIn{from{opacity:0;transform:translateY(14px) scale(.985)}to{opacity:1;transform:none}}
  .ico{
    width:56px;height:56px;border-radius:18px;margin:0 auto;display:grid;place-items:center;
    background:var(--grad);box-shadow:0 18px 36px -20px rgba(108,140,255,.9);
  }
  .ico svg{width:26px;height:26px;color:#fff}
  h1{margin:18px 0 0;font-size:19px;font-weight:800}
  p{margin:9px 0 0;font-size:14px;color:var(--muted)}
  a{
    display:flex;align-items:center;justify-content:center;width:100%;height:52px;margin-top:22px;
    border-radius:14px;color:#fff;text-decoration:none;font-size:16px;font-weight:800;
    background:var(--grad);box-shadow:0 16px 34px -20px rgba(108,140,255,.95);
    transition:transform .22s var(--spring);
  }
  a:active{transform:scale(.978)}
  @media (prefers-reduced-motion: reduce){*{animation-duration:.001ms!important;transition-duration:.001ms!important}}
</style>
</head>
<body>
  <div class="card">
    <div class="ico">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M7 8V4h10v4"/><path d="M7 17H5.5A1.5 1.5 0 0 1 4 15.5v-5A1.5 1.5 0 0 1 5.5 9h13A1.5 1.5 0 0 1 20 10.5v5a1.5 1.5 0 0 1-1.5 1.5H17"/><path d="M7 14h10v6H7z"/>
      </svg>
    </div>
    <h1>这个页面打不开</h1>
    <p>{{ message }}</p>
    <a href="/">返回首页</a>
  </div>
</body>
</html>
"""


def _is_api_request() -> bool:
    return request.path.startswith("/print/") or request.path.startswith("/status/")


def unlock_required(view):
    """所有打印和状态接口都必须先输入家庭访问密码。"""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            if _is_api_request():
                return jsonify(ok=False, need_password=True), 401
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped


def _friendly_api_failure():
    """前端只拿 ok=False，不暴露任何 Python 异常、英文或错误码。"""
    return jsonify(ok=False), 400


@app.after_request
def _set_safe_headers(response):
    # /qr.png 这类可长期缓存的资源已在路由里自带 Cache-Control，不覆盖。
    if "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"

    # ---- 首屏优化：HTML 页面走 ETag 协商缓存 + gzip 传输压缩 ----
    is_page = response.status_code == 200 and response.mimetype == "text/html"
    if is_page and not response.direct_passthrough:
        body = response.get_data()
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        response.headers["ETag"] = etag
        # 内容没变就不重复传整页 HTML（浏览器第二次打开瞬间完成）
        if request.headers.get("If-None-Match") == etag:
            response.status_code = 304
            response.set_data(b"")
            response.headers["Cache-Control"] = "no-cache"
            response.headers.pop("Pragma", None)
            response.headers.pop("Content-Length", None)
            return response
        response.headers["Cache-Control"] = "no-cache"
        response.headers.pop("Pragma", None)

        accept = request.headers.get("Accept-Encoding", "")
        if "gzip" in accept.lower() and len(body) > 1024:
            compressed = gzip.compress(body, 6)
            if len(compressed) < len(body):
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Vary"] = "Accept-Encoding"
    return response


@app.get("/")
def index():
    if not session.get("authed"):
        return _compiled_template(UNLOCK_HTML).render(
            error=None, url_for=url_for
        )
    # 二维码独立成 /qr.png 请求并长期缓存：首页 HTML 不再内嵌几 KB 的 base64 图片，
    # 第二次打开时浏览器直接用本地缓存的二维码，一毫秒都不用等。
    qr_version = WEB_QR_ETAG.strip('"')[:10]
    return _compiled_template(PAGE_HTML).render(
        qr_url="/qr.png?v=" + qr_version,
        lan_url=LAN_URL.split("://", 1)[-1],
        max_text_chars=MAX_TEXT_CHARS,
        max_image_bytes=IMAGE_MAX_BYTES,
        max_image_mb=max(1, round(IMAGE_MAX_BYTES / (1024 * 1024))),
        max_file_bytes=FILE_MAX_BYTES,
        max_file_mb=max(1, round(FILE_MAX_BYTES / (1024 * 1024))),
    )


@app.get("/qr.png")
def qr_png():
    """网页展示用二维码。内容不含隐私（和贴墙二维码一样），所以无需密码。"""
    if request.headers.get("If-None-Match") == WEB_QR_ETAG:
        response = app.response_class(status=304)
    else:
        response = app.response_class(WEB_QR_PNG_BYTES, mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    response.headers["ETag"] = WEB_QR_ETAG
    return response


@app.post("/unlock")
def unlock():
    submitted = request.form.get("password", "")
    submitted_bytes = str(submitted).encode("utf-8")
    password_bytes = str(ACCESS_PASSWORD).encode("utf-8")
    if hmac.compare_digest(submitted_bytes, password_bytes):
        session.clear()
        session.permanent = True
        session["authed"] = True
        return redirect(url_for("index"))

    # 轻微延迟，降低局域网里被人反复猜密码的速度。
    time.sleep(0.6)
    log.warning("访问密码错误，来源：%s", request.remote_addr)
    # 返回 200 而不是 401，避免个别手机浏览器把 401 当成“网页打不开”。
    return _compiled_template(UNLOCK_HTML).render(
        error="密码不对，请重新输入", url_for=url_for
    )


def _save_uploaded_image(upload) -> Path:
    """校验图片格式和大小，保存到 temp_print，返回临时文件路径。"""
    filename = upload.filename or ""
    extension = Path(filename).suffix.lower()
    if extension not in IMAGE_EXTS:
        raise ValueError("不支持的图片格式")

    data = upload.read(IMAGE_MAX_BYTES + 1)
    if not data:
        raise ValueError("图片为空")
    if len(data) > IMAGE_MAX_BYTES:
        raise ValueError("图片超过 8MB")

    # 不只看后缀，还用 Pillow 真正解码校验，防止损坏文件/伪装文件。
    try:
        with Image.open(io.BytesIO(data)) as image:
            actual_format = image.format
            image_width, image_height = image.size
            image.verify()
    except Exception as exc:
        raise ValueError("图片文件损坏或无法识别") from exc

    if image_width * image_height > MAX_IMAGE_PIXELS:
        raise ValueError("图片尺寸过大")

    suffix_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    suffix = suffix_map.get(actual_format or "")
    if not suffix:
        raise ValueError("只支持 jpg、png、webp 图片")

    target = TEMP_DIR / (uuid.uuid4().hex + suffix)
    try:
        target.write_bytes(data)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return target


@app.post("/print/text")
@unlock_required
def print_text():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _friendly_api_failure()

    text = payload.get("text")
    if not isinstance(text, str):
        return _friendly_api_failure()

    text = text.replace("\x00", "").strip()
    if not text or len(text) > MAX_TEXT_CHARS:
        return _friendly_api_failure()

    task_id = uuid.uuid4().hex
    txt_path = TEMP_DIR / (task_id + ".txt")
    try:
        txt_path.write_text(text, encoding="utf-8")
    except Exception:
        log.exception("保存文字临时文件失败")
        try:
            txt_path.unlink(missing_ok=True)
        except Exception:
            pass
        return _friendly_api_failure()

    try:
        _set_task_status(task_id, "queued")
        PRINT_QUEUE.put(
            PrintJob(task_id=task_id, kind="text", file_path=txt_path)
        )
    except Exception:
        log.exception("文字打印任务入队失败")
        try:
            txt_path.unlink(missing_ok=True)
        except Exception:
            pass
        return _friendly_api_failure()

    log.info("收到文字打印任务 %s，共 %s 字", task_id[:8], len(text))
    return jsonify(ok=True, task_id=task_id)


@app.post("/print/image")
@unlock_required
def print_image():
    upload = request.files.get("image")
    if upload is None:
        return _friendly_api_failure()

    try:
        image_path = _save_uploaded_image(upload)
    except Exception as exc:
        log.warning("图片校验未通过：%s", exc)
        return _friendly_api_failure()

    task_id = uuid.uuid4().hex
    # 临时文件名沿用任务号，便于排队和清理。
    final_path = TEMP_DIR / (task_id + image_path.suffix)
    try:
        image_path.replace(final_path)
    except Exception:
        log.exception("整理图片临时文件失败")
        try:
            image_path.unlink(missing_ok=True)
        except Exception:
            pass
        return _friendly_api_failure()

    try:
        _set_task_status(task_id, "queued")
        PRINT_QUEUE.put(
            PrintJob(task_id=task_id, kind="image", file_path=final_path)
        )
    except Exception:
        log.exception("图片打印任务入队失败")
        try:
            final_path.unlink(missing_ok=True)
        except Exception:
            pass
        return _friendly_api_failure()

    log.info("收到图片打印任务 %s", task_id[:8])
    return jsonify(ok=True, task_id=task_id)


@app.post("/print/file")
@unlock_required
def prepare_file():
    upload = request.files.get("file")
    if upload is None:
        return jsonify(ok=False, message="请先选择要打印的文件"), 400

    try:
        record, previews = _prepare_upload(upload)
    except UserFacingError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    except Exception:
        log.exception("准备上传文件失败")
        return (
            jsonify(ok=False, message="文件处理失败，请换一个文件再试"),
            400,
        )

    _store_prepared(record)
    log.info(
        "已准备文件 %s：%s，%s 页",
        record.file_id[:8],
        record.name,
        record.page_count,
    )
    return jsonify(
        ok=True,
        file_id=record.file_id,
        name=record.name,
        pages=record.page_count,
        preview_count=len(previews),
        previews=previews,
    )


@app.post("/print/file/<file_id>")
@unlock_required
def print_file(file_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", file_id or ""):
        return jsonify(ok=False, message="文件已过期，请重新选择"), 400

    record = _take_prepared(file_id)
    if record is None:
        return jsonify(ok=False, message="文件已过期，请重新选择"), 400

    task_id = uuid.uuid4().hex
    try:
        _set_task_status(task_id, "queued")
        PRINT_QUEUE.put(
            PrintJob(
                task_id=task_id,
                kind=record.kind,
                file_path=record.print_path,
                cleanup_paths=(record.work_dir,),
            )
        )
    except Exception:
        log.exception("文件打印任务入队失败")
        _delete_path(record.work_dir)
        return jsonify(ok=False, message="文件提交失败，请重新选择"), 400

    log.info("收到文件打印任务 %s：%s", task_id[:8], record.name)
    return jsonify(ok=True, task_id=task_id)


@app.get("/status/<task_id>")
@unlock_required
def task_status(task_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", task_id or ""):
        return jsonify(status="unknown", reason="")
    status, reason = _get_task_state(task_id)
    return jsonify(status=status, reason=reason)


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    if _is_api_request():
        return jsonify(ok=False), error.code or 400
    message = "页面不存在，请返回首页。" if error.code == 404 else "页面出错了，请返回首页重试。"
    return _compiled_template(ERROR_HTML).render(message=message), error.code or 400


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    log.exception("网页出现未处理异常")
    if _is_api_request():
        return jsonify(ok=False), 500
    return (
        _compiled_template(ERROR_HTML).render(
            message="页面出错了，请返回首页重试。"
        ),
        500,
    )


# =============================================================================
# 九、启动入口
# =============================================================================


def _configure_console() -> None:
    """尽量让 Windows 控制台正确显示中文和 emoji。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _export_wall_qr_async() -> None:
    """贴墙二维码生成+写盘放到后台线程，不拖慢“可以扫码”的时间。"""
    if not EXPORT_QR_IMAGE:
        return

    def job() -> None:
        global QR_PNG_BYTES
        try:
            png = build_qr_png(LAN_URL, box_size=32)
            QR_PNG_BYTES = png
            QR_WALL_PATH.write_bytes(png)
            print("✅ 墙上二维码图片已导出：" + QR_WALL_FILENAME)
        except Exception:
            print("⚠️ 墙上二维码图片导出失败，网页里的二维码仍然可以正常使用。")
            log.exception("导出 qr_wall.png 失败")

    threading.Thread(target=job, name="qr-wall", daemon=True).start()


def _probe_printer_async() -> None:
    """打印机探测放到后台线程：个别电脑上 spooler 卡顿时也不会拖慢启动。"""

    def job() -> None:
        try:
            default_printer = _get_default_printer()
        except Exception:
            log.exception("启动时检测默认打印机失败")
            default_printer = None
        if default_printer:
            print("✅ 默认打印机：" + str(default_printer))
        else:
            print("⚠️ 警告：没有检测到默认打印机，现在提交会打印失败。")
            print("   请打开：Windows 设置 → 蓝牙和其他设备 → 打印机和扫描仪，设置默认打印机。")

    threading.Thread(target=job, name="printer-probe", daemon=True).start()


def main() -> None:
    global ACCESS_PASSWORD, LAN_IP, LAN_URL, WEB_QR_PNG_BYTES, WEB_QR_ETAG

    _configure_console()
    ACCESS_PASSWORD = str(ACCESS_PASSWORD)
    if not ACCESS_PASSWORD:
        print("⚠️ 警告：访问密码为空，扫码后不用密码就能进入。请修改顶部 ACCESS_PASSWORD。")

    print()
    print("=" * 60)
    print("                  家庭内网打印工具")
    print("=" * 60)

    # 1. 临时目录
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print("❌ 无法创建临时文件夹 temp_print：" + str(exc))
        print("请把本程序放到有写入权限的目录（例如 D:\\家庭打印）后再运行。")
        raise SystemExit(1)

    # 2. 自动获取内网地址，拼接手机访问链接
    LAN_IP = detect_lan_ip()
    LAN_URL = "http://" + LAN_IP + ":" + str(PORT)
    if LAN_IP == "127.0.0.1":
        print("⚠️ 警告：没有检测到内网 IPv4 地址，二维码可能无法被手机打开。")
        print("   请先连接家里 WiFi，或重启本程序；也可以在代码顶部设置 LAN_IP_OVERRIDE。")

    # 3. 先把端口占住，再做剩下的准备工作。
    #    这样手机扫码哪怕快了一步，也会在连接队列里等几十毫秒后正常打开，
    #    而不是撞上“拒绝连接”；同时端口被占用也能第一时间报错。
    try:
        server = make_server(HOST, PORT, app, threaded=True)
    except (OSError, SystemExit) as exc:
        print("❌ 启动失败：端口 " + str(PORT) + " 可能已被占用：" + str(exc))
        print("请关闭占用该端口的程序，或修改代码顶部的 PORT。")
        raise SystemExit(1)

    # 4. 网页用小尺寸二维码（几毫秒），贴墙高清二维码丢到后台线程生成
    try:
        WEB_QR_PNG_BYTES = build_qr_png(LAN_URL, box_size=12)
        WEB_QR_ETAG = '"' + hashlib.md5(WEB_QR_PNG_BYTES).hexdigest() + '"'
    except Exception:
        server.server_close()
        print("❌ 二维码生成失败，请检查 qrcode/Pillow 是否安装正确。")
        log.exception("二维码生成失败")
        raise SystemExit(1)
    _export_wall_qr_async()

    # 5. 控制台关键信息（打印机探测在后台进行，结果马上会补一行输出）
    print()
    print("✅ 本机内网访问地址：" + LAN_URL)
    if not EXPORT_QR_IMAGE:
        print("ℹ️ 已关闭贴墙二维码导出（EXPORT_QR_IMAGE = False）。")
    print("✅ 访问密码：" + mask_password(ACCESS_PASSWORD))
    _probe_printer_async()
    print("✅ 提示：保持电脑开机、打印机开机才能正常使用")
    print("ℹ️ 首次运行如果手机打不开，请在 Windows 防火墙提示中允许“专用网络”。")
    if ENABLE_FRP_GUIDE:
        print("ℹ️ 外网访问教程在代码末尾注释里；网页只显示内网二维码。")
    print("ℹ️ 网页服务已就绪，正在后台完成最后的准备工作…")
    print("=" * 60)
    print()

    # 6. 启动唯一打印线程，然后开始提供网页服务
    worker = threading.Thread(target=_print_worker, name="print-worker", daemon=True)
    worker.start()

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n程序已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


# =============================================================================
# =============================================================================
#                         配套使用说明（代码末尾注释）
# =============================================================================
#
# 一、requirements.txt 完整依赖清单
# -----------------------------------------------------------------------------
# Flask>=3.0,<4
# pywin32>=306
# Pillow>=10.0
# qrcode[pil]>=7.4
# pypdfium2>=4.0
#
#
# 二、一键安装依赖命令
# -----------------------------------------------------------------------------
# 先安装 Python 3.10 或更高版本（安装时勾选 Add Python to PATH）。
# 打开命令提示符（cmd），进入本文件所在目录，执行：
#
#     pip install -r requirements.txt
#
# 如果还没有写 requirements.txt，也可以直接执行：
#
#     pip install Flask "pywin32>=306" "Pillow>=10.0" "qrcode[pil]>=7.4" "pypdfium2>=4.0"
#
# 安装完成后运行：
#
#     python print_server.py
#
# 注意：pywin32 只支持 Windows；本项目不需要也不能在 Linux/macOS 上打印。
#
#
# 三、修改配置项指引（在哪里改访问密码）
# -----------------------------------------------------------------------------
# 用记事本或 VS Code 打开本文件，找到最上面的“用户配置区”：
#
#     ACCESS_PASSWORD = "123456"
#
# 把 123456 改成家里自己的密码即可。改完保存，重新运行程序。
# 控制台只会显示脱敏后的密码，例如 1****6，不会显示完整密码。
#
# 其他常用配置也在同一区域：
#     PORT = 5000                  # 网页端口；被占用时可改成 5001
#     ENABLE_FRP_GUIDE = True      # 是否在控制台提示外网教程
#     EXPORT_QR_IMAGE = True       # 是否生成 qr_wall.png
#     LAN_IP_OVERRIDE = ""         # 多网卡时手工指定内网 IP
#     PAGE_MARGIN_MM = 20.0        # 打印页边距
#     TEXT_FONT_SIZE_PT = 24       # 文字字号
#     DELETE_AFTER_SECONDS = 30    # 任务结束后的临时文件保留秒数
#     FILE_MAX_BYTES = 52428800    # 手机文件/聊天文件上限 50MB
#     MAX_FILE_TEXT_CHARS = 50000  # 文字类文件最多 5 万字
#     FILE_PREVIEW_PAGES = 3       # 手机预览前 3 页
#     FILE_TTL_SECONDS = 1800      # 只预览未打印的文件 30 分钟后清理
#
# 手机文件/聊天文件说明：
#     - PDF：直接预览、打印，保留原版式；
#     - Word/Excel：调用电脑上安装的 Microsoft Office 转 PDF，再预览/打印；
#     - PPT：把每一页导出成图片，再按 A4 预览/打印，保留版式；
#     - TXT/HTML/JSON：按文字重新排版成 A4；
#     - jpg/png/webp：按图片规则适配 A4；
#     - 微信聊天文件需要在文件选择器里选“聊天文件”，网页本身不能直接读微信数据库。
#
# 修改密码后，已经登录的手机需要重新输入新密码（重启程序会自动让旧登录失效）。
#
#
# 四、frp 外网穿透完整教程（可选，让 4G 流量也能访问）
# -----------------------------------------------------------------------------
# 重要：网页界面永远只展示内网二维码。外网地址只给家里人自己用，不要贴墙上，
# 也不要在群里公开分享。
#
# 原理：
#     家里的 Windows 电脑运行 frpc（客户端）
#                  ↓ 主动连接
#     有公网 IP 的云服务器运行 frps（服务端）
#                  ↓
#     手机通过“公网IP:外网端口”访问家里的打印网页
#
# 1. 准备一台有公网 IP 的云服务器（Windows 或 Linux 都可以，推荐 Linux）。
#    在云厂商控制台的“安全组/防火墙”中放行：
#        - 7000/TCP（frp 服务端口，可自定义）
#        - 6000/TCP（外网访问打印网页的端口，可自定义）
#    如果使用域名 + 80/443，则改为放行 80/443，并自行配置 Nginx 反向代理。
#
# 2. 下载 frp：
#    官方地址：https://github.com/fatedier/frp/releases
#    选择最新版本的 Windows 压缩包，例如：
#        frp_0.61.0_windows_amd64.zip
#    解压后得到 frpc.exe、frpc.ini（新版可能是 frpc.toml）等文件。
#    服务端选择 linux_amd64 或 windows_amd64 对应包。
#    如果下载页打不开，也可以从国内镜像站搜索“frp releases”获取同版本文件。
#
# 3. 云服务器上的 frps.ini（服务端配置）示例：
#
#        [common]
#        bind_port = 7000
#        token = 换成一段很长的随机密码
#
#    启动服务端（Linux 示例）：
#
#        ./frps -c frps.ini
#
#    Windows 服务器则执行：
#
#        frps.exe -c frps.ini
#
# 4. 家里 Windows 电脑上的 frpc.ini（客户端配置）示例：
#
#        [common]
#        server_addr = 你的云服务器公网IP
#        server_port = 7000
#        token = 换成和 frps.ini 完全一样的随机密码
#
#        [family_print]
#        type = tcp
#        local_ip = 127.0.0.1
#        local_port = 5000
#        remote_port = 6000
#
#    注意：
#        - local_port 必须和本程序的 PORT 一致（默认 5000）。
#        - remote_port 是外网访问端口，不要和服务器上其他程序冲突。
#        - token 两边必须完全一致，且不要用 123456 这种弱密码。
#
# 5. 启动客户端（在家里 Windows 电脑上）：
#
#        frpc.exe -c frpc.ini
#
#    看到 start proxy success 之类的提示，就说明穿透成功。
#
# 6. 外网访问方式：
#
#        http://云服务器公网IP:6000
#
#    在手机 4G/5G 流量下打开这个地址，输入家庭访问密码，即可使用。
#    这个外网地址不要做成二维码贴墙，也不要随意发到群里。
#
# 7. 想让 frpc 开机自动运行：
#    可以把 frpc.exe 和 frpc.ini 放到固定目录，再按下面“Windows 开机自启”
#    的方法，为 frpc.exe 也创建一个任务计划程序任务。
#
# 8. 安全提醒：
#    - 一定要修改 ACCESS_PASSWORD，不要用 123456。
#    - frp 的 token 要足够长，云服务器安全组只放行必要端口。
#    - 外网暴露有风险；如果不需要外网访问，就不要启动 frpc。
#    - 更稳妥的做法是只在家里 WiFi 使用，不开启 frp。
#    - 公网长期使用建议用“域名 + Nginx + HTTPS”反向代理，避免密码明文传输。
#      本程序本身只监听 HTTP，HTTPS 由 Nginx/Caddy 负责。
#
#
# 五、Windows 开机自启方案（任务计划程序详细步骤）
# -----------------------------------------------------------------------------
# 说明：打印机和“默认打印机”通常跟登录用户绑定，建议选择“只在用户登录时运行”，
#       不要选“不管用户是否登录都运行”，否则可能找不到默认打印机。
#
# 1. 先确认 Python 路径。打开 cmd，执行：
#
#        where pythonw
#        where python
#
#    通常会得到类似：
#        C:\Users\你的用户名\AppData\Local\Programs\Python\Python312\pythonw.exe
#        C:\Users\你的用户名\AppData\Local\Programs\Python\Python312\python.exe
#
#    推荐先用 python.exe：会保留一个控制台窗口，能看到访问地址和出错原因。
#    确认长期运行稳定后，如果不想看到黑色窗口，再换成 pythonw.exe；
#    注意 pythonw.exe 没有控制台，出错时看不到日志。
#
# 2. 按 Win + R，输入 taskschd.msc，回车，打开“任务计划程序”。
#
# 3. 右侧点击“创建任务”（不是“创建基本任务”）。
#
# 4. “常规”选项卡：
#        - 名称：家庭打印服务
#        - 描述：开机自动运行家庭内网打印网页
#        - 选择“只在用户登录时运行”
#        - 勾选“使用最高权限运行”可选；如果打印机是当前用户安装的，不勾也可以
#        - “配置为”：Windows 10 / Windows 11
#
# 5. “触发器”选项卡 → 新建：
#        - 开始任务：登录时
#        - 延迟任务：30 秒（等网络和打印机就绪）
#        - 勾选“启用”
#
# 6. “操作”选项卡 → 新建：
#        - 操作：启动程序
#        - 程序或脚本：粘贴第 1 步查到的 python.exe（或 pythonw.exe）完整路径
#        - 添加参数：填本文件完整路径，例如：
#              "D:\家庭打印\print_server.py"
#          如果路径有空格，必须带英文双引号。
#        - 起始于：填本文件所在目录，例如：
#              D:\家庭打印
#
# 7. “条件”选项卡：
#        - 如果是台式机，可以取消“只有在使用交流电源时才启动此任务”
#        - 如果是笔记本，建议保持接通电源，避免打印中途休眠
#
# 8. “设置”选项卡：
#        - 勾选“如果任务失败，按以下频率重新启动”：1 分钟，最多 3 次
#        - 取消“如果任务运行时间超过以下时间，停止任务”（因为本程序要长期运行）
#        - 其他保持默认
#
# 9. 点“确定”保存。可以右键任务 →“运行”测试一次。
#    然后重启电脑，确认任务计划程序里显示“正在运行”。
#
# 10. 如果使用 pythonw.exe，桌面不会出现黑色控制台窗口；想查看运行日志，
#     可以先用 python.exe 手动运行排查问题，确认正常后再换成 pythonw.exe。
#
# 11. 电脑必须保持开机，不能休眠/睡眠；打印机必须开机、有纸、在线。
#     建议在“电源和睡眠”里把睡眠设为“从不”。
#
# 12. 如果手机打不开网页，按顺序检查：
#     - 手机和电脑是否连的同一个 WiFi；
#     - 控制台显示的 IP 是否正确（多网卡时可设置 LAN_IP_OVERRIDE）；
#     - Windows 防火墙是否允许 Python 访问“专用网络”；
#     - 路由器是否开启了“AP 隔离/访客网络隔离”（开启会导致设备互相访问不了）；
#     - 电脑 IP 是否变化。IP 变了要重新生成/打印 qr_wall.png。
#
#
# 六、安全提醒
# -----------------------------------------------------------------------------
# 1. 内网二维码只限家里使用。不要发到业主群、公司群、朋友圈。
# 2. 外网地址不要随意分享，防止别人恶意打印、浪费纸张，甚至打印不良内容。
# 3. ACCESS_PASSWORD 不要使用 123456、888888、生日、手机号后六位等弱密码。
# 4. 不需要外网访问时，不要启动 frpc，减少暴露面。
# 5. 打印机会执行真实的纸张输出，请把电脑放在家人能看管的位置。
# 6. 程序会把上传的图片、文字和文件临时存到 temp_print；
#    打印结束 30 秒后自动删除；只预览未打印的文件 30 分钟后自动删除；
#    如果程序被强制关闭，可能残留临时文件，可手动清空 temp_print。
# 7. 本程序没有用户注册、云端同步、OCR、微信机器人等功能，只做家庭内网打印。
#
#
# 七、给家人的简易使用说明
# -----------------------------------------------------------------------------
# 1. 手机连上家里的 WiFi（和打印电脑同一个 WiFi）。
# 2. 用微信扫墙上贴的二维码。
# 3. 输入家里的使用密码，点“进入打印页面”。
# 4. 想打印文字：在输入框里打字，点蓝色“打印文字”按钮。
#    想打印照片：点“选择手机相册图片打印”，选好照片，点蓝色“打印图片”按钮。
#    想打印文件/聊天文件：点“打印手机文件 / 微信聊天文件”里的文件框，
#    选择文件后点“预览文件”，确认预览效果，再点“确认打印这份文件”。
# 5. 看到“✅ 提交成功，正在排队打印”就是提交成功了。
#    如果看到“❌ 打印失败，请检查打印机开机、连线、缺纸”，
#    请叫家里人检查打印机是不是开机、有没有纸、线有没有插好。
# 6. 打印机会一张一张按顺序打印，不要连续狂点按钮。
# 7. 墙上的二维码只能用家里 WiFi 扫；在外面用手机流量是打不开的。
#
# =============================================================================
# 常见问题（给维护者看的）
# =============================================================================
# Q1：控制台提示没有默认打印机？
# A1：Windows 设置 → 蓝牙和其他设备 → 打印机和扫描仪 → 点你的打印机 →
#     设为默认值。然后重新提交任务。
#
# Q2：网页能打开，但打印没反应？
# A2：先确认打印机开机、在线、有纸；再看控制台错误日志；
#     也可以先用记事本随便打印一页，确认 Windows 自身能打印。
#
# Q3：手机扫码打不开？
# A3：确认同一个 WiFi、IP 正确、防火墙放行、路由器没有开启 AP 隔离。
#     电脑 IP 变化后，需要重新运行程序生成新的 qr_wall.png 并重新打印。
#
# Q4：想改端口？
# A4：修改顶部 PORT = 5000，保存后重启；frpc.ini 的 local_port 也要同步修改。
#
# Q5：为什么文字不是直接调用记事本打印？
# A5：为了保证 A4、宋体、24 号、固定页边距、自动分页和左对齐稳定一致，
#     程序先把文字写入临时 txt，再用 Pillow 排成 A4 页面，最后通过 pywin32
#     调用 Windows 默认打印机。临时 txt 在打印结束 30 秒后删除。
#
# Q6：手机文件/聊天文件支持哪些格式？
# A6：PDF、Word（doc/docx/rtf/odt）、Excel（xls/xlsx/csv/ods）、
#     PPT（ppt/pptx/odp）、TXT/HTML/JSON、图片（jpg/png/webp）。
#     Word/Excel/PPT 需要电脑安装 Microsoft Office；没有 Office 时请先另存为 PDF。
#
# Q7：微信聊天记录里的文件怎么选？
# A7：网页不能直接读取微信聊天数据库。点文件选择框后，在微信/手机弹出的
#     文件选择器里选择“聊天文件”或“手机文件”；如果版本没有这个入口，
#     先在微信里把文件保存到手机，再从这里选择。
#
# Q8：文件预览后为什么还要点一次“确认打印”？
# A8：文件可能有多页，先让手机看到实际页面效果，避免老人误打几十页。
#     预览前 3 页，确认按钮会打印全部页面。
# =============================================================================
