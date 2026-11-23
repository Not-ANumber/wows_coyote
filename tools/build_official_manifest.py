# -*- coding: utf-8 -*-
"""从用户安装/提供的 dglab-kit-python 生成官方模板清单。

不会把波形数据写进项目仓库；输出文件由用户决定放在哪里。
生成的 manifest 带 source/license/commit/sha256，供 standalone 的
`--templates manifest --template-manifest <file>` 使用。

用法：
  py -3.13 tools/build_official_manifest.py --out official_manifest.json
  py -3.13 tools/build_official_manifest.py --sdk-path "C:\\...\\dglab-kit-python" --out official_manifest.json

注意：dglab-kit-python 是 GPL-3.0；如果项目不是 GPL 兼容，不要把生成的
raw 数据随项目分发，只在本地使用。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from standalone.official_templates import (  # noqa: E402
    TemplateLoadError, load_from_sdk,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成官方模板 manifest")
    parser.add_argument("--sdk-path", default="",
                        help="dglab-kit-python 源码目录（含 src/）或 coyote.py")
    parser.add_argument("--out", default="official_manifest.json",
                        help="输出 manifest 路径")
    parser.add_argument("--commit", default="",
                        help="固定 SDK 的 git commit；默认尝试从 --sdk-path 读取")
    args = parser.parse_args(argv)

    try:
        library = load_from_sdk(args.sdk_path or None)
    except TemplateLoadError as exc:
        print("加载官方 SDK 失败：{}".format(exc), file=sys.stderr)
        return 2

    commit = args.commit.strip()
    if not commit and args.sdk_path:
        commit = _git_commit(args.sdk_path)
    if not commit:
        commit = library.commit or "source"

    manifest = {
        "schema_version": 1,
        "source": library.source,
        "license": library.license,
        "commit": commit,
        "waves": [library.templates[name].to_manifest()
                  for name in library.names()],
    }
    out = os.path.abspath(os.path.expanduser(args.out))
    directory = os.path.dirname(out)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print("已生成 {} 条模板：{}".format(len(library), out))
    print("source={} license={} commit={}".format(
        library.source, library.license, commit))
    if library.license:
        print("提醒：{} 数据；如项目不是 GPL 兼容，不要把该文件随项目分发。".format(
            library.license))
    return 0


def _git_commit(path: str) -> str:
    """从 SDK 源码目录读取 HEAD；没有 git 时返回空串。"""
    workdir = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(workdir):
        workdir = os.path.dirname(workdir)
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", workdir,
             "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


if __name__ == "__main__":
    sys.exit(main())
