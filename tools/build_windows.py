# -*- coding: utf-8 -*-
"""构建 Windows standalone 可执行包。

前置：
  py -3.13 -m pip install -r standalone/requirements.txt
  py -3.13 -m pip install pyinstaller

用法：
  py -3.13 tools/build_windows.py
  py -3.13 tools/build_windows.py --overlay   # 额外构建独立 overlay exe

输出：
  dist/wows_coyote/wows_coyote.exe
  dist/wows_coyote/wows_overlay.exe      （--overlay 且安装 pywebview）
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPS = os.path.join(ROOT, ".phase0deps")


def _project_version() -> str:
    path = os.path.join(ROOT, "standalone", "__init__.py")
    with open(path, "r", encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)', handle.read())
    if not match:
        raise SystemExit("无法读取版本号：{}".format(path))
    return match.group(1)


VERSION = _project_version()
DIST_DIR = os.path.join(ROOT, "dist", "wows_coyote")


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    if os.path.isdir(DEPS):
        env["PYTHONPATH"] = DEPS + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run(cmd: list[str]) -> int:
    print("+", " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT, env=_build_env())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Windows 可执行包")
    parser.add_argument("--overlay", action="store_true",
                        help="同时构建 wows_overlay.exe（需要 pywebview）")
    parser.add_argument("--no-source-zip", action="store_true",
                        help="不重新生成 GPL 源码 zip")
    args = parser.parse_args(argv)

    if importlib.util.find_spec("PyInstaller") is None:
        print("缺少 PyInstaller，请先运行：", file=sys.stderr)
        print("  py -3.13 -m pip install pyinstaller", file=sys.stderr)
        return 2
    if importlib.util.find_spec("webview") is None:
        print("警告：未安装 pywebview，主程序将回退浏览器模式；"
              "安装：py -3.13 -m pip install -r standalone/requirements.txt",
              file=sys.stderr)

    if not args.no_source_zip:
        code = _run([sys.executable, os.path.join(
            ROOT, "tools", "build_standalone_zip.py")])
        if code != 0:
            return code

    code = _run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        os.path.join(ROOT, "standalone", "wows_coyote.spec"),
    ])
    if code != 0:
        return code

    exe = os.path.join(
        ROOT, "dist", "wows_coyote", "wows_coyote.exe")
    if not os.path.isfile(exe):
        print("构建结束但未找到：{}".format(exe), file=sys.stderr)
        return 3
    print("主程序：{}".format(exe))

    if args.overlay:
        if importlib.util.find_spec("webview") is None:
            print("未安装 pywebview，跳过 wows_overlay.exe。")
            print("安装后重新运行：py -3.13 -m pip install pywebview")
        else:
            code = _run([
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                os.path.join(ROOT, "standalone", "wows_overlay.spec"),
            ])
            if code != 0:
                return code
            overlay = os.path.join(ROOT, "dist", "wows_overlay.exe")
            print("Overlay：{}".format(overlay))

    # 让许可证/源码在分发目录里可见；overlay exe 放到主程序旁边。
    for name in ("LICENSE", "THIRD_PARTY.md", "SOURCE_OFFER.md",
                 "CHANGELOG.md"):
        source = os.path.join(ROOT, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(DIST_DIR, name))
    readme = os.path.join(ROOT, "standalone", "README.md")
    if os.path.isfile(readme):
        shutil.copy2(readme, os.path.join(DIST_DIR, "README.md"))
    source_zip = os.path.join(
        ROOT, "releases", "wows_coyote-{}-gpl.zip".format(VERSION))
    if os.path.isfile(source_zip):
        os.makedirs(os.path.join(DIST_DIR, "source"), exist_ok=True)
        shutil.copy2(source_zip, os.path.join(
            DIST_DIR, "source", os.path.basename(source_zip)))
    overlay_exe = os.path.join(ROOT, "dist", "wows_overlay.exe")
    if os.path.isfile(overlay_exe):
        shutil.copy2(overlay_exe, os.path.join(DIST_DIR, "wows_overlay.exe"))

    # 便携 zip（解压即用；GPL 源码在 source/ 内）。
    portable = os.path.join(
        ROOT, "releases", "wows_coyote-{}-win64.zip".format(VERSION))
    os.makedirs(os.path.dirname(portable), exist_ok=True)
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(DIST_DIR):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, DIST_DIR)
                zf.write(path, rel.replace("\\", "/"))
    digest = hashlib.sha256(open(portable, "rb").read()).hexdigest()
    with open(portable + ".sha256", "w", encoding="ascii") as handle:
        handle.write("{}  {}\n".format(digest, os.path.basename(portable)))
    print("可执行包：{}".format(portable))
    print("可执行包 SHA256：{}".format(digest))
    print("分发目录：{}".format(DIST_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
