# -*- coding: utf-8 -*-
"""准备一个 standalone 发布版本：校验版本、构建、刷新校验值、生成 latest.json。

用法：
  py -3.13 tools/make_release.py
  py -3.13 tools/make_release.py --version 0.1.1
  py -3.13 tools/make_release.py --skip-build
  py -3.13 tools/make_release.py --repo Not-ANumber/wows_coyote

默认从 standalone/__init__.py 读取版本；构建流程与 docs/standalone更新分发.md
一致，**不自动上传**，只打印 GitHub Release 和服务器清单的上传命令。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REPO = os.environ.get("WOWS_RELEASE_REPO", "Not-ANumber/wows_coyote")
INNO_CANDIDATES = (
    os.environ.get("ISCC", ""),
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
UPDATE_MANIFEST = os.path.join(ROOT, "releases", "latest.json")


def project_version() -> str:
    path = os.path.join(ROOT, "standalone", "__init__.py")
    with open(path, "r", encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)', handle.read())
    if not match:
        raise SystemExit("无法读取版本号：{}".format(path))
    return match.group(1)


def sha256(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


def find_iscc() -> str:
    for candidate in INNO_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    found = shutil.which("ISCC") or shutil.which("iscc")
    return found or ""


def _read(path: str) -> str:
    try:
        return open(path, "r", encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def check_versions(version: str, notes_file: str,
                   github_notes_file: str) -> list[str]:
    problems: list[str] = []
    current = project_version()
    if current != version:
        problems.append("standalone/__init__.py 是 {}，期望 {}".format(
            current, version))

    iss = _read(os.path.join(ROOT, "installer", "wows_coyote.iss"))
    match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss)
    if not match:
        problems.append("installer/wows_coyote.iss 找不到 MyAppVersion")
    elif match.group(1) != version:
        problems.append("installer/wows_coyote.iss 默认版本是 {}，期望 {}".format(
            match.group(1), version))

    changelog = _read(os.path.join(ROOT, "CHANGELOG.md"))
    if "## standalone {}".format(version) not in changelog:
        problems.append("CHANGELOG.md 缺少 '## standalone {}' 段落".format(
            version))

    for label, path in (("更新说明", notes_file),
                        ("GitHub 发布说明", github_notes_file)):
        if version not in _read(path):
            problems.append("{} 未写版本 {}：{}".format(
                label, version, os.path.relpath(path, ROOT)))

    for label, path in (("README.md", os.path.join(ROOT, "README.md")),
                        ("releases/README.md",
                         os.path.join(ROOT, "releases", "README.md"))):
        if "wows_coyote-{}-".format(version) not in _read(path):
            problems.append("{} 未引用 wows_coyote-{}-*".format(label, version))
    return problems


def build(version: str) -> int:
    code = run([sys.executable, os.path.join("tools", "build_windows.py")])
    if code != 0:
        return code
    iscc = find_iscc()
    if not iscc:
        print("找不到 Inno Setup 的 ISCC.exe。")
        print("请安装 Inno Setup 6/7，或设置 ISCC 环境变量后重试。")
        return 3
    code = run([iscc, "/DMyAppVersion={}".format(version),
                os.path.join("installer", "wows_coyote.iss")])
    if code != 0:
        return code
    return run([sys.executable, os.path.join("tools", "finalize_release.py"),
                "--version", version])


def artifact_paths(version: str) -> tuple[str, str, str]:
    return (
        os.path.join(ROOT, "releases",
                     "wows_coyote-{}-gpl.zip".format(version)),
        os.path.join(ROOT, "releases",
                     "wows_coyote-{}-win64.zip".format(version)),
        os.path.join(ROOT, "dist_installer",
                     "wows_coyote-{}-setup.exe".format(version)),
    )


def make_manifest(version: str, repo: str, notes_file: str) -> dict:
    gpl, portable, setup = artifact_paths(version)
    missing = [path for path in (gpl, portable, setup)
               if not os.path.isfile(path)]
    if missing:
        raise SystemExit("缺少发布物，先运行构建：\n  " + "\n  ".join(missing))

    notes = _read(notes_file).strip()
    if not notes:
        notes = "wows_coyote {}".format(version)
    if len(notes) > 8000:
        notes = notes[:8000].rstrip() + "…"

    setup_name = os.path.basename(setup)
    gpl_name = os.path.basename(gpl)
    base = "https://github.com/{}/releases/download/v{}".format(repo, version)
    published = dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    data = {
        "schema": 1,
        "version": version,
        "published_at": published,
        "notes": notes,
        "notes_url": "https://github.com/{}/releases/tag/v{}".format(
            repo, version),
        "installer": {
            "name": setup_name,
            "size": os.path.getsize(setup),
            "sha256": sha256(setup),
            "urls": [base + "/" + setup_name],
        },
        "source_url": base + "/" + gpl_name,
    }
    os.makedirs(os.path.dirname(UPDATE_MANIFEST), exist_ok=True)
    with open(UPDATE_MANIFEST, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    # 用客户端同一份解析器验证，避免生成客户端读不了的清单。
    sys.path.insert(0, ROOT)
    from standalone.update import parse_manifest
    release = parse_manifest(open(UPDATE_MANIFEST, "rb").read())
    if release.version != version:
        raise SystemExit("清单版本校验失败")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备 standalone 发布")
    parser.add_argument("--version", default=project_version())
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help="GitHub 仓库，默认 Not-ANumber/wows_coyote")
    parser.add_argument("--notes", default=os.path.join(
        ROOT, "dist_installer", "UPDATE_NOTES.md"),
        help="更新提示里的简短说明")
    parser.add_argument("--github-notes", default=os.path.join(
        ROOT, "dist_installer", "GITHUB_RELEASE_NOTES.md"),
        help="GitHub Release 正文")
    parser.add_argument("--skip-build", action="store_true",
                        help="只用现有产物，重新生成校验值/清单")
    args = parser.parse_args(argv)

    version = args.version.strip()
    if not VERSION_RE.match(version):
        print("版本号必须是 X.Y.Z：{}".format(version))
        return 2
    if "/" not in args.repo or " " in args.repo:
        print("--repo 必须是 OWNER/REPO：{}".format(args.repo))
        return 2

    problems = check_versions(version, args.notes, args.github_notes)
    if problems:
        print("版本一致性检查未通过：")
        for problem in problems:
            print("  - " + problem)
        return 2

    if not args.skip_build:
        code = build(version)
        if code != 0:
            return code
    else:
        missing = [path for path in artifact_paths(version)
                   if not os.path.isfile(path)]
        if missing:
            print("缺少发布物：")
            for path in missing:
                print("  " + path)
            return 2

    data = make_manifest(version, args.repo, args.notes)
    gpl, portable, setup = artifact_paths(version)
    print("\n发布物：")
    for path in (gpl, portable, setup):
        print("  {:>8.1f} MiB  {}  {}".format(
            os.path.getsize(path) / 1048576,
            sha256(path)[:16] + "…", os.path.relpath(path, ROOT)))
    print("\n更新清单：{}".format(os.path.relpath(UPDATE_MANIFEST, ROOT)))
    print("  version={}  installer={}  sha256={}…".format(
        data["version"], data["installer"]["name"],
        data["installer"]["sha256"][:16]))

    repo = args.repo
    tag = "v" + version
    print("\n下一步（不自动执行）：")
    print("1) 上传 GitHub Release：")
    print("   gh release create {} \\".format(tag))
    for path in (setup, setup + ".sha256", gpl, portable,
                 os.path.join(ROOT, "releases", "SHA256SUMS.txt")):
        print("     {} \\".format(os.path.relpath(path, ROOT)))
    print("     --title \"wows_coyote {}\" \\".format(version))
    print("     --notes-file {}".format(
        os.path.relpath(args.github_notes, ROOT)))
    print("2) 核对下载文件的 SHA256 与清单一致。")
    print("3) 原子替换服务器清单：")
    print("   WOWS_UPDATE_HOST=root@<服务器IP> "
          "tools/deploy_manifest.sh releases/latest.json")
    print("4) 0.1.0 用户没有更新检查，需通知其手动安装 {} 一次。".format(
        version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
