# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-dir build for the standalone host.

Build from project root:
  py -3.13 -m PyInstaller --noconfirm --clean standalone/wows_coyote.spec
"""

import importlib.util
import os
import re

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
HAS_WEBVIEW = importlib.util.find_spec("webview") is not None
ICON_PATH = os.path.join(ROOT, "assets", "brand", "wows-icon.ico")


def project_version():
    path = os.path.join(ROOT, "standalone", "__init__.py")
    with open(path, "r", encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)',
                          handle.read())
    if not match:
        raise SystemExit("无法读取版本号：{}".format(path))
    return match.group(1)


VERSION = project_version()

WEBVIEW_IMPORTS = [
    "webview",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr_loader",
    "pythonnet",
    "bottle",
    "proxy_tools",
] if HAS_WEBVIEW else []


def add(datas, source, dest):
    if os.path.exists(source):
        datas.append((source, dest))


datas = []
add(datas, os.path.join(ROOT, "wows_coyote", "main.py"), "wows_coyote")
add(datas, os.path.join(ROOT, "wows_coyote", "manifest.json"), "wows_coyote")
add(datas, os.path.join(ROOT, "wows_coyote", "base.json"), "wows_coyote")
add(datas, os.path.join(ROOT, "wows_coyote", "hit.json"), "wows_coyote")
add(datas, os.path.join(ROOT, "wows_coyote", "wows_preset_v2.json"), "wows_coyote")
add(datas, os.path.join(ROOT, "wows_coyote", "README.md"), "wows_coyote")
add(datas, os.path.join(ROOT, "standalone", "templates"), "standalone/templates")
add(datas, os.path.join(ROOT, "standalone", "presets"), "standalone/presets")
add(datas, os.path.join(ROOT, "standalone", "ui"), "standalone/ui")
add(datas, os.path.join(ROOT, "standalone", "README.md"), "standalone")
add(datas, os.path.join(ROOT, "standalone", "requirements.txt"), "standalone")
add(datas, os.path.join(ROOT, "LICENSE"), ".")
add(datas, os.path.join(ROOT, "THIRD_PARTY.md"), ".")
add(datas, os.path.join(ROOT, "SOURCE_OFFER.md"), ".")
add(datas, os.path.join(
    ROOT, "releases", "wows_coyote-{}-gpl.zip".format(VERSION)), "source")
add(datas, os.path.join(ROOT, "docs", "官方SDK模板用法.md"), "docs")
add(datas, os.path.join(ROOT, "docs", "standalone界面定稿.md"), "docs")
add(datas, os.path.join(ROOT, "docs", "standalone_V4.md"), "docs")

a = Analysis(
    [os.path.join(ROOT, "standalone", "run.py")],
    pathex=[ROOT, os.path.join(ROOT, ".phase0deps")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "websockets",
        "websockets.asyncio.server",
        "websockets.asyncio.client",
        "websockets.exceptions",
        "qrcode",
        "qrcode.image.svg",
        # wows_coyote/main.py 是运行时按文件加载的 data，PyInstaller 不会分析它的 import。
        "logging.handlers",
        "urllib.request",
        "asyncio",
        "json",
        "math",
        "re",
        "tempfile",
        "threading",
        "time",
    ] + WEBVIEW_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="wows_coyote",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="wows_coyote",
)
