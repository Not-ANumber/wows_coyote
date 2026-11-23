# -*- coding: utf-8 -*-
"""刷新发布校验值和发布说明，避免手工改哈希。

用法：
  py -3.13 tools/finalize_release.py
  py -3.13 tools/finalize_release.py --version 1.0.0

会更新：
  releases/wows_coyote-<ver>-gpl.zip.sha256
  releases/wows_coyote-<ver>-win64.zip.sha256
  releases/SHA256SUMS.txt
  dist_installer/wows_coyote-<ver>-setup.exe.sha256
  dist_installer/GITHUB_RELEASE_NOTES.md（SHA256 与体积）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _project_version() -> str:
    path = os.path.join(ROOT, "standalone", "__init__.py")
    with open(path, "r", encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)', handle.read())
    if not match:
        raise SystemExit("无法读取版本号：{}".format(path))
    return match.group(1)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha_file(path: str) -> str:
    digest = sha256(path)
    with open(path + ".sha256", "w", encoding="ascii") as handle:
        handle.write("{}  {}\n".format(digest, os.path.basename(path)))
    return digest


def update_sums(entries: list[tuple[str, str]]) -> None:
    path = os.path.join(ROOT, "releases", "SHA256SUMS.txt")
    lines: list[str] = []
    if os.path.isfile(path):
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    wanted = {name for name, _digest in entries}
    kept: list[str] = []
    for line in lines:
        match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
        if match and match.group(2).strip() in wanted:
            continue
        kept.append(line)
    for name, digest in entries:
        kept.append("{}  {}".format(digest, name))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept) + "\n")


def update_notes(notes_path: str, setup_hash: str, size_mib: float) -> None:
    if not os.path.isfile(notes_path):
        print("跳过发布说明（不存在）：{}".format(notes_path))
        return
    text = open(notes_path, encoding="utf-8").read()
    text = re.sub(r"\b[0-9a-fA-F]{64}\b", setup_hash, text)
    text = re.sub(r"（约 \d+(?:\.\d+)? MiB）",
                  "（约 {:.1f} MiB）".format(size_mib), text)
    with open(notes_path, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷新发布校验值")
    parser.add_argument("--version", default=_project_version())
    parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)

    version = args.version
    gpl = os.path.join(
        ROOT, "releases", "wows_coyote-{}-gpl.zip".format(version))
    portable = os.path.join(
        ROOT, "releases", "wows_coyote-{}-win64.zip".format(version))
    setup = os.path.join(
        ROOT, "dist_installer", "wows_coyote-{}-setup.exe".format(version))
    missing = [path for path in (gpl, portable, setup)
               if not os.path.isfile(path)]
    if missing:
        for path in missing:
            print("缺少发布物：{}".format(path))
        print("请先运行 tools/build_windows.py 并编译安装包。")
        return 2

    init_path = os.path.join(ROOT, "standalone", "__init__.py")
    if os.path.isfile(init_path):
        match = re.search(r"__version__\s*=\s*[\"']([^\"']+)",
                          open(init_path, encoding="utf-8").read())
        if match and match.group(1) != version:
            print("警告：standalone/__init__.py 版本是 {}，"
                  "与发布版本 {} 不一致".format(match.group(1), version))

    hashes = [
        (os.path.basename(gpl), write_sha_file(gpl)),
        (os.path.basename(portable), write_sha_file(portable)),
    ]
    setup_hash = write_sha_file(setup)
    update_sums(hashes)
    size_mib = os.path.getsize(setup) / (1024 * 1024)
    notes = args.notes or os.path.join(
        ROOT, "dist_installer", "GITHUB_RELEASE_NOTES.md")
    update_notes(notes, setup_hash, size_mib)

    print("已刷新校验值：")
    for name, digest in hashes:
        print("  {}  {}".format(digest, name))
    print("  {}  {}".format(setup_hash, os.path.basename(setup)))
    print("发布说明：{}（{:.1f} MiB）".format(notes, size_mib))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
