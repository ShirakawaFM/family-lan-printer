# -*- coding: utf-8 -*-
"""
构建 Release 的两个压缩包：LAN 局域网版 与 公网部署版。

用法：
    python tools/build_release.py v1.2.0

产出（写到 dist/）：
    family-lan-printer-lan-<tag>.zip       不含 deploy-public
    family-lan-printer-public-<tag>.zip    含 deploy-public
    release-notes.md                       含两个包的 SHA256

为什么不用 PowerShell 的 Compress-Archive：它写入 zip 的路径分隔符是反斜杠，
不符合 zip 规范（应为 /），在 macOS / Linux 上解压会生成名字里带反斜杠的文件。
这里统一用 zipfile 写入正斜杠，任何平台解压都正常。
"""

import hashlib
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COMMON_FILES = [
    "print_server.py",
    "requirements.txt",
    "README.md",
    "README_EN.md",
    "CHANGELOG.md",
    "LICENSE",
]
COMMON_DIRS = ["assets", "docs"]
PACK_LAN = Path("pack/lan/先看-局域网版.txt")
PACK_PUBLIC = Path("pack/public/先看-公网版.txt")
PUBLIC_EXTRA_DIR = "deploy-public"

NOTES_TEMPLATE = """## 两个版本，按需下载

| 版本 | 适合谁 | 下载 |
| --- | --- | --- |
| **LAN 局域网版** | 只在家里同一个 WiFi 下用，最简单，不需要云服务器 | `{lan_name}` |
| **公网部署版** | 需要在外面用 4G/5G 访问，需要一台云服务器 | `{pub_name}` |

两个包都包含完整的 `print_server.py`、`requirements.txt`、中英文 README 和截图。
公网部署版额外包含 `deploy-public/`：填写单、云服务器/Windows 一键脚本和可选 HTTPS 教程。

不确定就选 **LAN 局域网版**。

### 文件校验

- `{lan_name}`  
  SHA256: `{lan_sum}`
- `{pub_name}`  
  SHA256: `{pub_sum}`

### 说明

- LAN 版不需要云服务器；公网版需要云服务器和 frp，按包里的 `deploy-public/00-先看-公网部署教程.md` 操作。
- 公网 HTTP 的密码不是加密传输，长期使用请按 `optional-https` 加 HTTPS，并务必修改 `ACCESS_PASSWORD`。
- 完整更新日志见 `CHANGELOG.md`。
"""


# 固定时间戳：同样的内容永远得到同样的 zip，SHA256 才是可验证的。
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def put(zf: zipfile.ZipFile, arc: str, data: bytes) -> None:
    """按正斜杠路径、固定时间戳写入一个文件。"""
    info = zipfile.ZipInfo(filename=arc, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data, compresslevel=9)


def add_tree(zf: zipfile.ZipFile, arc_root: str, rel_dir: str) -> int:
    """把目录按正斜杠路径写进 zip，返回写入的文件数。"""
    base = ROOT / rel_dir
    count = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        arc = "{}/{}/{}".format(arc_root, rel_dir, path.relative_to(base).as_posix())
        put(zf, arc, path.read_bytes())
        count += 1
    return count


def add_quick_start(zf: zipfile.ZipFile, arc_root: str, src: Path, tag: str) -> None:
    text = (ROOT / src).read_text(encoding="utf-8").replace("{VER}", tag)
    # 先统一成 LF，再转成 CRLF，避免 .gitattributes 检出成 CRLF 时变成 \r\r\n
    body = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    # UTF-8 BOM：Windows 记事本双击打开才能正确识别中文
    put(zf, "{}/{}".format(arc_root, src.name), b"\xef\xbb\xbf" + body.encode("utf-8"))


def build(tag: str, dist: Path) -> tuple:
    dist.mkdir(parents=True, exist_ok=True)
    results = []

    variants = [
        ("family-lan-printer-lan", False),
        ("family-lan-printer-public", True),
    ]
    for arc_root, with_public in variants:
        zip_path = dist / "{}-{}.zip".format(arc_root, tag)
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for name in COMMON_FILES:
                put(zf, "{}/{}".format(arc_root, name), (ROOT / name).read_bytes())
            for rel_dir in COMMON_DIRS:
                add_tree(zf, arc_root, rel_dir)
            if with_public:
                add_tree(zf, arc_root, PUBLIC_EXTRA_DIR)
                add_quick_start(zf, arc_root, PACK_PUBLIC, tag)
            else:
                add_quick_start(zf, arc_root, PACK_LAN, tag)
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest().upper()
        results.append((zip_path.name, digest, zip_path.stat().st_size, with_public))

    lan = results[0]
    pub = results[1]
    notes = NOTES_TEMPLATE.format(
        lan_name=lan[0], lan_sum=lan[1], pub_name=pub[0], pub_sum=pub[1]
    )
    (dist / "release-notes.md").write_text(notes, encoding="utf-8")
    # CI 用：把校验和写成 KEY=VALUE，供 GITHUB_ENV 直接追加
    (dist / "sums.env").write_text(
        "LAN_SUM={}\nPUB_SUM={}\n".format(lan[1], pub[1]), encoding="utf-8"
    )

    return lan, pub


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：python tools/build_release.py <tag>，例如 v1.2.0")
        return 2
    tag = sys.argv[1].strip()
    if not tag:
        print("tag 不能为空")
        return 2

    lan, pub = build(tag, ROOT / "dist")
    for name, digest, size, with_public in (lan, pub):
        entries = "含 deploy-public" if with_public else "不含 deploy-public"
        print("  {}  {:.0f} KB  {}  {}".format(name, size / 1024, entries, digest))
    print("  release-notes.md 已生成")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
