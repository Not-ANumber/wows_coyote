# -*- coding: utf-8 -*-
"""构建 standalone 的 GPL-3.0 源码分发包。

包含随包官方模板 manifest、LICENSE、THIRD_PARTY、SOURCE_OFFER、
standalone/ 源码和它依赖的 wows_coyote 引擎文件。

用法：
  py -3.13 tools/build_standalone_zip.py
  py -3.13 tools/build_standalone_zip.py --version 1.0.0 --out releases/wows_coyote-1.0.0-gpl.zip
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {"__pycache__", ".phase0deps", ".git", "archive", "ref",
                "releases"}
EXCLUDE_FILES = {"qr.svg", "app.log"}
EXCLUDE_SUFFIXES = (".pyc", ".log", ".tmp")

ROOT_FILES = ("LICENSE", "THIRD_PARTY.md", "SOURCE_OFFER.md", "CHANGELOG.md")
BRAND_FILES = ("wows-icon.ico", "mark-yellow.svg", "mark-black.svg",
               "icon-black-yellow.svg", "icon-yellow-black.svg",
               "generate.py", "README.md")
ENGINE_FILES = ("main.py", "manifest.json", "base.json", "hit.json",
                "wows_preset_v2.json", "README.md")
TOOL_FILES = ("build_official_manifest.py", "build_standalone_zip.py",
              "build_windows.py", "audit_release.py",
              "finalize_release.py", "make_release.py",
              "deploy_manifest.sh", "create_clean_test_user.ps1",
              "remove_clean_test_user.ps1")
INSTALLER_FILES = ("wows_coyote.iss", "README.md", "ChineseSimplified.isl")
DOC_FILES = ("官方SDK模板用法.md", "官方波形模板调研.md",
             "standalone界面定稿.md", "standalone_V4.md",
             "standalone发布指南.md", "初次使用完整流程.md",
             "P0发布检查清单.md", "干净环境安装测试.md",
             "本地标准用户干净环境测试.md", "VirtualBox干净环境测试.md")

README_TEXT = (
    "wows_coyote (GPL-3.0)\n"
    "=================================\n\n"
    "默认使用随包官方模板（dglab-kit-python，24 条）。\n\n"
    "安装依赖：\n"
    "  py -3.13 -m pip install -r standalone/requirements.txt\n\n"
    "运行：\n"
    "  py -3.13 standalone/run.py --open-browser\n"
    "  py -3.13 standalone/run.py --transport v4\n"
    "  py -3.13 standalone/run.py --transport v4 --v4-relay local\n\n"
    "强度上限以 App/设备通道上限为准；软件强度上限设置已取消。\n"
    "V3 为本机 Socket；V4 为 Relay 控制方（official）或本机中继（local）。\n"
    "不需要 clone SDK；--templates sdk 仅用于直接读取 SDK 源码。\n"
    "详细说明见 standalone/README.md 和 docs/standalone_V4.md。\n"
    "许可/源码声明见 LICENSE、THIRD_PARTY.md、SOURCE_OFFER.md。\n"
)


def _project_version() -> str:
    path = os.path.join(ROOT, "standalone", "__init__.py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            match = re.search(r'__version__\s*=\s*["\']([^"\']+)',
                              handle.read())
    except OSError as exc:
        raise SystemExit("无法读取版本号：{}".format(exc))
    if not match:
        raise SystemExit("无法读取版本号：{}".format(path))
    return match.group(1)


def _wanted(path: str) -> bool:
    """发布集过滤：运行时产物、日志、缓存一律不进包。"""
    if not os.path.isfile(path):
        return False
    if os.path.basename(path) in EXCLUDE_FILES:
        return False
    if path.lower().endswith(EXCLUDE_SUFFIXES):
        return False
    return True


def _tree_entries(base: str, arcbase: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [name for name in dirnames if name not in EXCLUDE_DIRS]
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            if not _wanted(path):
                continue
            rel = os.path.relpath(path, base)
            entries.append((path, os.path.join(arcbase, rel).replace("\\", "/")))
    return entries


def _listed_entries(root: str, base: str, arcbase: str,
                    names: tuple[str, ...]) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for name in names:
        path = os.path.join(root, base, name)
        if _wanted(path):
            entries.append((path, arcbase + "/" + name if arcbase else name))
    return entries


def collect_entries(root: str = ROOT) -> list[tuple[str, str]]:
    """返回源码包的 (源文件, 包内路径) 清单；构建与审计共用。"""
    entries = _listed_entries(root, "", "", ROOT_FILES)
    entries += _tree_entries(os.path.join(root, "standalone"), "standalone")
    # 品牌图标：构建 spec 与安装包引用此目录，源码包必须能完整重建。
    entries += _listed_entries(root, "assets/brand", "assets/brand",
                               BRAND_FILES)
    entries += _listed_entries(root, "wows_coyote", "wows_coyote",
                               ENGINE_FILES)
    entries += _listed_entries(root, "tools", "tools", TOOL_FILES)
    entries += _listed_entries(root, "installer", "installer",
                               INSTALLER_FILES)
    entries += _listed_entries(root, "docs", "docs", DOC_FILES)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 standalone GPL 源码包")
    parser.add_argument("--version", default=_project_version())
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    out = args.out or os.path.join(
        ROOT, "releases", "wows_coyote-{}-gpl.zip".format(args.version))
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        # 发布包根目录的 README：直接给出运行方式和许可，避免相对链接失效。
        zf.writestr("README.md", README_TEXT)
        for path, arcname in collect_entries():
            zf.write(path, arcname)

    digest = hashlib.sha256(open(out, "rb").read()).hexdigest()
    with open(out + ".sha256", "w", encoding="ascii") as handle:
        handle.write("{}  {}\n".format(digest, os.path.basename(out)))
    print("已生成：{}".format(out))
    print("SHA256：{}".format(digest))
    print("包含随包 manifest：{}".format(
        "standalone/templates/official_manifest.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
