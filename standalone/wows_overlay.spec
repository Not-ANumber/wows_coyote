# -*- mode: python ; coding: utf-8 -*-
"""Optional one-file overlay exe (requires pywebview).

Build from project root:
  py -3.13 -m PyInstaller --noconfirm --clean standalone/wows_overlay.spec

If pywebview isn't installed, the main UI still opens /overlay in a browser
popup; this exe only adds frameless always-on-top behavior.
"""

import importlib.util
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
HAS_WEBVIEW = importlib.util.find_spec("webview") is not None
ICON_PATH = os.path.join(ROOT, "assets", "brand", "wows-icon.ico")

a = Analysis(
    [os.path.join(ROOT, "standalone", "overlay.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=(["webview", "standalone.desktop"]
                   if HAS_WEBVIEW else []),
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
    a.binaries,
    a.datas,
    [],
    name="wows_overlay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON_PATH if os.path.isfile(ICON_PATH) else None,
)
