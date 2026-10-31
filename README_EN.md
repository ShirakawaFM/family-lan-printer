<div align="center">

<img src="assets/banner-en.svg" alt="Family LAN Print: scan with WeChat, print text, photos and files" width="100%" />

**A dead-simple home print server for Windows. Phones on the same Wi-Fi scan a QR code, enter a shared password, and print text, photos, or files from the phone / WeChat chat history.**

English · [简体中文](README.md)

[![Version](https://img.shields.io/badge/version-1.1.0-8B5CF6?style=flat-square&logo=github&logoColor=white)](CHANGELOG.md)
[![CI](https://github.com/ShirakawaFM/family-lan-printer/actions/workflows/python-check.yml/badge.svg)](https://github.com/ShirakawaFM/family-lan-printer/actions/workflows/python-check.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?style=flat-square&logo=windows&logoColor=white)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/license-MIT-22c55e?style=flat-square)](LICENSE)
[![Issues](https://img.shields.io/github/issues/ShirakawaFM/family-lan-printer?style=flat-square&color=F59E0B)](https://github.com/ShirakawaFM/family-lan-printer/issues)
[![Stars](https://img.shields.io/github/stars/ShirakawaFM/family-lan-printer?style=social)](https://github.com/ShirakawaFM/family-lan-printer/stargazers)

</div>

> This is **not** cloud printing. Text, images and files stay on your Windows PC and are deleted 30 seconds after each print job. Files that are only previewed are cleaned up after 30 minutes. No accounts, no telemetry, no ads.

---

## Three steps to run it

<div align="center">
<code>① install deps</code>&nbsp; → &nbsp;<code>② set the password</code>&nbsp; → &nbsp;<code>③ start &amp; scan</code>
</div>

```bat
:: 1) deps (once)
python -m pip install -r requirements.txt

:: 2) open print_server.py and change ACCESS_PASSWORD (default: 123456)

:: 3) start the server, print qr_wall.png and stick it next to the printer
python print_server.py
```

No app, no sign-up, no internet needed: **join the same Wi-Fi → scan the QR with WeChat → enter the password → print.** Full walkthrough in [Quick start](#quick-start) below.

<div align="center">
<a href="#why">💡 Why</a>&nbsp; · &nbsp;<a href="#preview">🖼️ Preview</a>&nbsp; · &nbsp;<a href="#features">✨ Features</a>&nbsp; · &nbsp;<a href="#how-it-works">⚙️ How it works</a>&nbsp; · &nbsp;<a href="#configuration">🔧 Config</a>&nbsp; · &nbsp;<a href="#faq">❓ FAQ</a>&nbsp; · &nbsp;<a href="README.md">🇨🇳 中文</a>
</div>

---

## Why

The printer is connected to a Windows PC at home. Printing a photo or a note usually means: find the PC, find the file, open it, click print. Elderly parents and children cannot do that alone.

This project reduces it to:

1. Run one Python file on the Windows PC.
2. Print `qr_wall.png` and stick it next to the printer.
3. Phone on the same Wi-Fi: scan → enter password → type or choose a photo → tap print.

## Preview

### Desktop · dark theme (follows the system by default)

> The QR code in the screenshots is a demo link (`http://192.168.1.100:5000`), not a real address.

<div align="center">
<a href="docs/screenshot-print.png"><img src="docs/screenshot-print.png" alt="Desktop print page (dark theme)" width="820" /></a>
</div>

<details>
<summary><b>Light theme / mobile / password page / splash</b></summary>

<br>

<table>
<tr>
<td align="center" width="50%">
<a href="docs/screenshot-print-light.png"><img src="docs/screenshot-print-light.png" alt="Desktop print page (light theme)" width="100%" /></a>
<br><sub><b>Desktop · light theme</b> — toggle in the top-right corner, follows the system preference</sub>
</td>
<td align="center" width="50%">
<a href="docs/screenshot-splash.png"><img src="docs/screenshot-splash.png" alt="Branded splash animation" width="100%" /></a>
<br><sub><b>Branded splash</b> — ~1 s, fades out as soon as the page is ready</sub>
</td>
</tr>
<tr>
<td align="center" width="50%">
<a href="docs/screenshot-login.png"><img src="docs/screenshot-login.png" alt="Password page" width="60%" /></a>
<br><sub><b>Password page</b> — one shared family password, no sign-up</sub>
</td>
<td align="center" width="50%">
<a href="docs/screenshot-print-mobile.png"><img src="docs/screenshot-print-mobile.png" alt="Mobile print page" width="60%" /></a>
<br><sub><b>Mobile print page</b> — the QR card collapses into a strip so the input is visible immediately</sub>
</td>
</tr>
</table>

</details>

The UI uses a modern, motion-rich style: dark/light themes (follows the system, manual toggle included), gradient glow background, glassy cards, tabbed print modes and spring micro-interactions. A ~1 s branded splash animation plays on the print page and fades out as soon as the page is ready. Buttons stay large and full-width (56 px tall, 16 px+ font) so touch use by kids and grandparents is unaffected.

## Features

- **QR access** — detects the LAN IPv4 automatically, serves the page at `http://<ip>:5000`, and exports a print-ready `qr_wall.png`.
- **Shared password** — one family password, not a user account system. The console only prints a masked version.
- **A4 text printing** — SimSun, 24 pt, left aligned, 20 mm margins, automatic wrapping and pagination.
- **Photo printing** — jpg / png / webp, max 8 MB; EXIF orientation fixed, aspect ratio preserved, centered on A4, no stretching or cropping.
- **Phone / WeChat chat files** — pick PDF, Word, Excel, PowerPoint, TXT, HTML or images from the phone or the WeChat file picker. PDF/Office keep their original layout; text is reflowed.
- **Print preview** — the first 3 pages are rendered as real page thumbnails; the user must confirm before all pages are printed.
- **Serial print queue** — one worker consumes `queue.Queue`; jobs never print concurrently.
- **Auto cleanup** — every temp file is deleted 30 seconds after its job ends; preview-only files expire after 30 minutes.
- **Friendly errors** — the browser shows the likely cause (paper out, paper jam, offline, door open, no toner, etc.) and a matching fix; stack traces and English error codes stay in the console.
- **Optional remote access** — a complete frp tutorial is included at the end of the Python file. The web UI never displays an external QR code.
- **Single-file deployment** — no frontend build, database, Docker, or cloud dependency.
- **One-click-ish setup** — `pip install -r requirements.txt`, then `python print_server.py`.

**Out of scope:** OCR, WeChat bots, cloud sync, Wi-Fi setup QR codes, user registration, layout controls.

## How it works

```text
Phone / WeChat browser
        │ HTTP on the same Wi-Fi
        ▼
Flask 0.0.0.0:5000
  ├─ GET  /              password page / print page
  ├─ POST /unlock        shared password → session
  ├─ POST /print/text    text → temp_print/<task>.txt
  ├─ POST /print/image   validated image → temp_print/<task>.<ext>
  ├─ POST /print/file    file → temp preview (PDF/Office/TXT/HTML/image)
  ├─ POST /print/file/id confirm and queue the prepared file
  └─ GET  /status/<id>   queued / printing / done / error
        │
        ▼
queue.Queue (FIFO)
        ▼
single print-worker thread
  ├─ text: Pillow → A4 pages
  ├─ image: EXIF + contain + center on A4
  ├─ PDF: pypdfium2 render pages
  ├─ Office: COM → PDF → render pages
  ├─ PPT: Slides.Export → PNG → A4 pages
  └─ pywin32 / win32ui → Windows default printer
        │
        ▼
temp file deleted 30 s after the job ends
```

The single-consumer queue exists for a reason: concurrent `StartDoc` calls can confuse spooler/driver combinations, reorder jobs, or lock up the printer. One worker means strict FIFO.

## Quick start

### Requirements

| Item | Requirement |
| --- | --- |
| OS | Windows 10 / 11 |
| Python | 3.10+ (tested on 3.14) |
| Python deps | Flask, Pillow, qrcode, pypdfium2, pywin32 |
| Printer | installed in Windows and set as **default** |
| Network | PC and phone on the same Wi-Fi |
| Paper | A4 |
| Office | Microsoft Office is required for Word/Excel/PPT layout conversion; PDF/TXT/HTML/images work without it |

### Install

```bat
cd /d D:\family-print
python -m pip install -r requirements.txt
```

### Change the password

In `print_server.py`:

```python
ACCESS_PASSWORD = "123456"
```

Change it before exposing the service to your family. `123456` is only a first-run default.

### Run

```bat
python print_server.py
```

The console prints:

```text
✅ LAN URL: http://192.168.1.100:5000
✅ Wall QR exported: qr_wall.png
✅ Access password: 1****6
✅ Keep the PC and printer powered on.
```

Allow Python through Windows Firewall for **Private networks** when prompted. Keep the console window open.

### Print the wall QR

Print `qr_wall.png` from the project folder and stick it next to the printer. Scan with WeChat, enter the password, and print.

## Configuration

All values live at the top of `print_server.py`:

| Option | Default | Description |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | Listen on all interfaces |
| `PORT` | `5000` | HTTP port |
| `ACCESS_PASSWORD` | `123456` | Shared family password; change it |
| `ENABLE_FRP_GUIDE` | `True` | Console hint for the frp tutorial; never shows an external QR |
| `EXPORT_QR_IMAGE` | `True` | Write `qr_wall.png` on startup |
| `QR_WALL_FILENAME` | `qr_wall.png` | Wall QR file name |
| `LAN_IP_OVERRIDE` | `""` | Manual LAN IPv4 for multi-NIC/VPN machines |
| `TEMP_DIR_NAME` | `temp_print` | Temp directory |
| `PAGE_MARGIN_MM` | `20.0` | A4 margin in millimetres |
| `TEXT_FONT_SIZE_PT` | `24` | Text size in points |
| `TEXT_LINE_SPACING` | `1.5` | Line-height multiplier |
| `RENDER_DPI` | `300` | Page render DPI |
| `IMAGE_MAX_BYTES` | `8388608` | 8 MB image limit |
| `MAX_IMAGE_PIXELS` | `50000000` | Pixel limit to avoid memory blow-ups |
| `MAX_TEXT_CHARS` | `20000` | Text limit per job |
| `FILE_MAX_BYTES` | `52428800` | Phone/chat file limit, 50 MB |
| `FILE_PREVIEW_PAGES` | `3` | Pages shown in the phone preview |
| `FILE_PREVIEW_DPI` | `96` | Preview render DPI |
| `PDF_PRINT_DPI` | `200` | PDF print render DPI |
| `MAX_FILE_TEXT_CHARS` | `50000` | Text-file limit |
| `FILE_TTL_SECONDS` | `1800` | Unprinted preview lifetime |
| `DELETE_AFTER_SECONDS` | `30` | Temp-file retention after a job |
| `SESSION_LIFETIME_DAYS` | `30` | Login session lifetime |

## Print rules

### Text

- A4 portrait
- SimSun 24 pt (falls back to Microsoft YaHei / DengXian / SimHei)
- Left aligned
- Fixed 20 mm margins
- Automatic line wrapping and pagination
- No layout options in the browser

### Images

- jpg / jpeg / png / webp, max 8 MB
- EXIF orientation corrected
- Aspect ratio preserved, scaled to fit the A4 content area
- Centered, never cropped or stretched
- Transparent areas flattened onto white

### Printer

- Uses the Windows default printer only.
- Best-effort preflight status check for paused, error, paper jam, paper out, offline, no toner, door open, etc.
- Some drivers do not report status; in that case the spooler result is all we can know.
- Set the printer paper size to A4.

## Security

- Shared password + signed session cookie; no user registration.
- The wall QR contains only the LAN URL.
- The web UI never renders a public/external QR code.
- Temp files are deleted 30 seconds after each job.
- No cloud services, no third-party APIs, no telemetry.
- Frontend errors are fixed Chinese messages; details go to the console.
- Uploaded images are checked by extension, size, real Pillow format, and pixel count.

If you expose this through frp, use a strong password, a strong frp token, and HTTPS (Nginx/Caddy). Plain HTTP over the public internet exposes the password in transit.

## Public deployment pack (optional)

The web UI intentionally only shows the LAN QR. For 4G/5G access, use the ready-made pack in [`deploy-public/`](deploy-public/README.md) instead of writing frp configs by hand.

The pack contains:

- [`01-先填-部署信息.txt`](deploy-public/01-先填-部署信息.txt): fill in every `（）` placeholder;
- [`server/02-云服务器-运行这个.sh`](deploy-public/server/02-云服务器-运行这个.sh): one-shot frps installer for Ubuntu;
- [`windows/03-家里电脑-运行这个.ps1`](deploy-public/windows/03-家里电脑-运行这个.ps1): downloads frpc, writes config, creates a startup shortcut;
- [`00-先看-公网部署教程.md`](deploy-public/00-先看-公网部署教程.md): full walkthrough and troubleshooting;
- [`optional-https/HTTPS-可选教程.md`](deploy-public/optional-https/HTTPS-可选教程.md): optional Caddy HTTPS.

Quick flow: open `7000/tcp` and `6000/tcp` in the cloud firewall, run the server script, run the Windows script with the same token, then open `http://YOUR_PUBLIC_SERVER_IP:6000` on 4G.

> Plain HTTP exposes the password in transit. Use the optional HTTPS guide for long-term use, and change `ACCESS_PASSWORD` before exposing anything.

## Windows autostart

Use Task Scheduler instead of the Startup folder:

1. `Win + R` → `taskschd.msc`.
2. Create Task (not Create Basic Task).
3. General: run only when the user is logged on.
4. Trigger: at logon, delay 30 seconds.
5. Action: start `python.exe` or `pythonw.exe`.
6. Arguments: `"D:\family-print\print_server.py"`.
7. Start in: `D:\family-print`.
8. Allow restart on failure; disable the "stop task if it runs longer than" option.

Start with `python.exe` so you can see the console. Switch to `pythonw.exe` only after everything works.

## Project layout

```text
family-lan-printer/
├─ print_server.py          # Flask app, print worker, queue, HTML UI, docs comments
├─ requirements.txt
├─ README.md
├─ README_EN.md
├─ LICENSE
├─ CHANGELOG.md
├─ assets/                       # animated SVG for READMEs
│  ├─ banner.svg / banner-en.svg
│  ├─ pipeline.svg
│  ├─ divider.svg
│  └─ footer.svg
├─ docs/screenshot-login.png
├─ docs/screenshot-print.png
├─ deploy-public/             # public deployment pack and fill-in sheets
└─ .github/workflows/python-check.yml
```

Runtime-only files (ignored by Git):

```text
temp_print/
qr_wall.png
frp/, frpc.toml, frpc.log
server.log
server.err.log
```

## Known limitations

- Windows only; uses `pywin32` and the Windows printing API.
- Default printer only; no printer picker.
- Printer status is best-effort and driver-dependent.
- A4 portrait layout only; set the printer paper size to A4.
- If the PC's LAN IP changes, regenerate and reprint `qr_wall.png`.
- `pythonw.exe` hides the console and therefore the logs.
- Phone and PC must be on the same LAN; guest Wi-Fi / AP isolation will block access.
- Word/Excel/PPT need Microsoft Office installed for layout-preserving conversion; otherwise save as PDF first.
- The browser cannot read the WeChat chat database directly. The file picker can only open files through the phone/WeChat document picker.
- Preview shows the first 3 pages only; printing outputs all pages.
- Text and chat exports are reflowed; PDF/Office/PPT keep their original layout.

## FAQ

**`ERR_CONNECTION_REFUSED` on the phone.** The server is not running or the console was closed. Open `http://127.0.0.1:5000` on the PC first, then use the LAN URL shown in the console and check Windows Firewall.

**PC opens the page but the phone does not.** Check same Wi-Fi, AP isolation, firewall, and `LAN_IP_OVERRIDE` for multi-NIC/VPN machines.

**No default printer.** Set it in Windows Settings → Bluetooth & devices → Printers & scanners, then print a test page from Notepad.

**Photo upload fails.** Only jpg/png/webp up to 8 MB. Convert HEIC photos to jpg first.

**File preview fails / Office cannot open.** Make sure the file opens in Word/Excel/PowerPoint on the PC. Without Microsoft Office, save it as PDF first. PDF/TXT/HTML/images do not need Office.

**Where are WeChat chat files?** After tapping the file input, look for the "Chat files" / "Files" entry in the phone picker. If the current WeChat version does not show it, save the file to the phone first.

**Why does preview show only 3 pages?** To keep the phone UI fast and prevent accidental long prints. The title shows the total page count; confirming prints all pages.

**Why no "print completed" message?** Windows spooler accepts a job before paper comes out. The UI only promises "queued" or "failed", not a fake completion event.

## Development

```bat
python -m py_compile print_server.py
python print_server.py
```

CI runs `py_compile` on a Windows runner. It does not print, because GitHub Actions has no printer.

## Contributing

Issues and pull requests are welcome. Please keep the project single-file, avoid frontend builds/databases/cloud services, and do not add out-of-scope features such as OCR, bots, or user accounts.

## License

[MIT](LICENSE) © 2026 ShirakawaFM

## Disclaimer

This is a personal home tool, not a supported commercial product. Use it at your own risk. The author is not responsible for failed prints, wasted paper, driver issues, network exposure, or data loss.
