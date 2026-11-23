# -*- coding: utf-8 -*-
"""发布前审计：发布集内容、敏感信息、许可证文件和校验和。

用法：
  py -3.13 tools/audit_release.py
  py -3.13 tools/audit_release.py --no-archives
  py -3.13 tools/audit_release.py --archive releases/wows_coyote-1.0.0-gpl.zip
  py -3.13 tools/audit_release.py --strict

审计范围：
  1. 源码发布集：与 build_standalone_zip.collect_entries 完全一致；
  2. releases/*.zip：源码包、Windows 便携包（含内嵌源码包）；
  3. dist_installer/*.exe：安装包原始字节；
  4. releases/SHA256SUMS.txt 与各 *.sha256 校验文件。

退出码：0 通过；1 有 FAIL（--strict 时 REVIEW 也算失败）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from build_standalone_zip import ROOT, collect_entries  # noqa: E402


def _project_version() -> str:
    path = os.path.join(ROOT, "standalone", "__init__.py")
    with open(path, "r", encoding="utf-8") as handle:
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)', handle.read())
    if not match:
        raise SystemExit("无法读取版本号：{}".format(path))
    return match.group(1)

HIGH_PATTERNS = (
    ("私钥", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("赋值型密钥/口令", re.compile(
        r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*"
        r"[\"']([^\"'\s]{6,})[\"']")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("OpenAI Key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("配对 targetId", re.compile(r"\btid=[A-Za-z0-9_-]{6,}")),
    ("开发机绝对路径", re.compile(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:wowsDG|mymod)"
        r"(?![A-Za-z0-9])")),
)

REVIEW_PATTERNS = (
    ("Windows 用户路径", re.compile(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z]:[\\/]Users[\\/]([^\\/\s\"'`]+)")),
    ("邮箱", re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
)

# 文档里的固定测试账户名；它不是个人信息，不需要逐条告警。
SAFE_USERS = {"wows_clean", "public", "default", "default user",
              "all users", "wdagutilityaccount"}
# 第三方 Inno Setup 中文语言文件里的译者公开署名，不是本项目秘密。
SAFE_EMAILS = {"847320916@qq.com"}
QR_PLACEHOLDERS = {"targetid", "target_id", "your_target_id"}

TEXT_SUFFIXES = (".py", ".md", ".json", ".txt", ".html", ".css", ".js",
                 ".spec", ".iss", ".ps1", ".isl", ".svg", ".bat", ".cfg",
                 ".ini", ".toml", ".yml", ".yaml", ".xml", ".csv")
BINARY_SUFFIXES = (".exe", ".dll", ".pyd", ".zip", ".ico", ".png", ".jpg")

FORBIDDEN_NAMES = {
    "qr.svg", "settings.json", ".env", "id_rsa", "id_ed25519",
    "app.log", "wows_coyote.log",
}
FORBIDDEN_SUFFIXES = (".log", ".pyc", ".pyo", ".pem", ".key", ".pfx",
                      ".p12", ".db", ".sqlite", ".sqlite3", ".bak",
                      ".tmp", ".swp")
FORBIDDEN_DIRS = {"__pycache__", ".git", ".svn", ".hg", ".venv", "venv",
                  "node_modules", ".phase0deps", ".testdeps"}

SOURCE_REQUIRED = (
    "LICENSE",
    "THIRD_PARTY.md",
    "SOURCE_OFFER.md",
    "standalone/run.py",
    "standalone/requirements.txt",
    "standalone/templates/official_manifest.json",
    "standalone/presets/default_events.json",
    "wows_coyote/main.py",
    "installer/wows_coyote.iss",
)
PORTABLE_REQUIRED = (
    "wows_coyote.exe",
    "LICENSE",
    "THIRD_PARTY.md",
    "SOURCE_OFFER.md",
    "_internal/standalone/templates/official_manifest.json",
    "_internal/standalone/presets/default_events.json",
    "_internal/wows_coyote/main.py",
)

MAX_REPORT_PER_FILE = 12


class Report:
    def __init__(self, strict: bool) -> None:
        self.strict = strict
        self.fail = 0
        self.review = 0
        self.info = 0

    def add(self, level: str, where: str, what: str, detail: str = "") -> None:
        if level == "FAIL":
            self.fail += 1
        elif level == "REVIEW":
            self.review += 1
        else:
            self.info += 1
        suffix = "  ({})".format(detail) if detail else ""
        print("  [{}] {}  {}{}".format(level, where, what, suffix))

    def ok(self) -> bool:
        return self.fail == 0 and not (self.strict and self.review)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _scan_text(report: Report, where: str, text: str) -> None:
    shown = 0
    for label, pattern in HIGH_PATTERNS:
        for match in pattern.finditer(text):
            if shown >= MAX_REPORT_PER_FILE:
                return
            if label == "配对 targetId":
                value = match.group(0).split("=", 1)[-1].lower()
                if value in QR_PLACEHOLDERS:
                    continue
            if label == "赋值型密钥/口令":
                value = match.group(2).strip().lower()
                if ("%(" in value or "{{" in value or "${" in value
                        or value.startswith(("<", "..."))
                        or value in {"changeme", "placeholder",
                                     "your_token", "your-token",
                                     "your_password", "your-password"}):
                    continue
            line = _line_of(text, match.start())
            report.add("FAIL", "{}:L{}".format(where, line), label,
                       match.group(0)[:120])
            shown += 1
    for label, pattern in REVIEW_PATTERNS:
        for match in pattern.finditer(text):
            if label == "Windows 用户路径":
                name = match.group(1).strip().lower()
                if name in SAFE_USERS or name.startswith(("<", "%", ".")):
                    continue
            if label == "邮箱" and match.group(0).lower() in SAFE_EMAILS:
                continue
            if label == "邮箱" and ".dist-info/" in where.lower():
                # 第三方包元数据里的维护者邮箱，属于公开署名。
                continue
            if shown >= MAX_REPORT_PER_FILE:
                return
            line = _line_of(text, match.start())
            report.add("REVIEW", "{}:L{}".format(where, line), label,
                       match.group(0)[:120])
            shown += 1


def _scan_bytes(report: Report, where: str, data: bytes) -> None:
    """二进制里只查最关键的 ASCII 特征，避免误报和过慢。"""
    checks = (
        ("开发机绝对路径", re.compile(
            rb"(?i)(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:wowsDG|mymod)"
            rb"(?![A-Za-z0-9])")),
        ("Windows 用户路径", re.compile(
            rb"(?i)(?<![A-Za-z0-9])[A-Za-z]:[\\/]Users[\\/]([^\\/\s\"'`]+)")),
        ("配对 targetId", re.compile(rb"\btid=[A-Za-z0-9_-]{6,}")),
    )
    for label, pattern in checks:
        for match in pattern.finditer(data):
            text = match.group(0)[:120].decode("latin-1", "replace")
            if label == "Windows 用户路径":
                name = text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()
                if name in SAFE_USERS or name.startswith(("<", "%", ".")):
                    continue
            report.add("FAIL", where, label, text)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _check_name(report: Report, where: str, name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    for part in parts[:-1]:
        if part in FORBIDDEN_DIRS:
            report.add("FAIL", where, "禁止目录 {}".format(part))
            return False
    base = parts[-1]
    if base in FORBIDDEN_NAMES:
        report.add("FAIL", where, "运行时/敏感文件 {}".format(base))
        return False
    if base.lower().endswith(FORBIDDEN_SUFFIXES):
        report.add("FAIL", where, "禁止后缀 {}".format(base))
        return False
    return True


def _scan_source_set(report: Report) -> list[tuple[str, str]]:
    entries = collect_entries(ROOT)
    print("\n[源码发布集] {} 个文件".format(len(entries)))
    for path, arcname in entries:
        if not _check_name(report, arcname, arcname):
            continue
        try:
            data = open(path, "rb").read()
        except OSError as exc:
            report.add("FAIL", arcname, "无法读取", str(exc))
            continue
        text = _decode(data)
        if text is not None:
            _scan_text(report, arcname, text)
        else:
            _scan_bytes(report, arcname, data)
    return entries


def _scan_zip(report: Report, zip_path: str, label: str,
              depth: int = 0) -> None:
    print("\n[归档] {} ({})".format(zip_path, label))
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        report.add("FAIL", zip_path, "无法打开", str(exc))
        return
    with zf:
        names = zf.namelist()
        for required in (SOURCE_REQUIRED if label == "source"
                         else PORTABLE_REQUIRED):
            if required not in names:
                report.add("FAIL", zip_path, "缺少必需文件 {}".format(required))
        if label == "portable":
            if not any(re.fullmatch(r"source/wows_coyote-.*-gpl\.zip", n)
                       for n in names):
                report.add("FAIL", zip_path, "缺少 GPL 对应源码 zip")
        for info in zf.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if not _check_name(report, name, name):
                continue
            try:
                data = zf.read(info)
            except (OSError, RuntimeError) as exc:
                report.add("FAIL", name, "无法读取", str(exc))
                continue
            if name.lower().endswith(".zip") and depth < 2:
                # 只递归检查内嵌的 GPL 源码包；base_library.zip 等
                # PyInstaller 运行时归档不按发布文件规则检查。
                if "-gpl" not in name.lower():
                    continue
                import tempfile
                handle = tempfile.NamedTemporaryFile(
                    suffix=".zip", delete=False)
                try:
                    handle.write(data)
                    handle.close()
                    _scan_zip(report, handle.name, "source", depth + 1)
                finally:
                    try:
                        os.unlink(handle.name)
                    except OSError:
                        pass
                continue
            text = _decode(data)
            if text is not None:
                _scan_text(report, name, text)
            elif name.lower().endswith(BINARY_SUFFIXES):
                _scan_bytes(report, name, data)


def _scan_raw_file(report: Report, path: str) -> None:
    print("\n[安装包] {}".format(path))
    try:
        data = open(path, "rb").read()
    except OSError as exc:
        report.add("FAIL", path, "无法读取", str(exc))
        return
    _scan_bytes(report, os.path.basename(path), data)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_sums(report: Report) -> None:
    print("\n[校验和]")
    releases_dir = os.path.join(ROOT, "releases")
    if not os.path.isdir(releases_dir):
        print("  未找到 releases/，跳过（在解压后的源码树内属正常）")
        return
    sums_path = os.path.join(releases_dir, "SHA256SUMS.txt")
    checked = 0
    if os.path.isfile(sums_path):
        for line in open(sums_path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
            if not match:
                report.add("REVIEW", "releases/SHA256SUMS.txt",
                           "无法解析的行", line[:80])
                continue
            expected, name = match.group(1).lower(), match.group(2).strip()
            target = os.path.join(ROOT, "releases", name)
            if not os.path.isfile(target):
                report.add("FAIL", "releases/SHA256SUMS.txt",
                           "文件不存在 {}".format(name))
                continue
            actual = _sha256(target)
            checked += 1
            if actual != expected:
                report.add("FAIL", "releases/" + name, "SHA256 不匹配",
                           "期望 {}… 实际 {}…".format(
                               expected[:12], actual[:12]))
    else:
        report.add("REVIEW", "releases/SHA256SUMS.txt", "文件不存在")

    for folder in ("releases", "dist_installer"):
        base = os.path.join(ROOT, folder)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if not name.endswith(".sha256"):
                continue
            path = os.path.join(base, name)
            text = open(path, encoding="ascii", errors="ignore").read().strip()
            match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", text)
            if not match:
                report.add("REVIEW", name, "无法解析的校验文件")
                continue
            expected, target_name = match.group(1).lower(), match.group(2).strip()
            target = os.path.join(base, target_name)
            if not os.path.isfile(target):
                report.add("FAIL", name, "目标文件不存在", target_name)
                continue
            actual = _sha256(target)
            checked += 1
            if actual != expected:
                report.add("FAIL", name, "SHA256 不匹配",
                           "期望 {}… 实际 {}…".format(
                               expected[:12], actual[:12]))
    print("  已核对 {} 个校验值".format(checked))


def _check_manifest(report: Report, version: str) -> None:
    path = os.path.join(ROOT, "releases", "latest.json")
    setup = os.path.join(
        ROOT, "dist_installer", "wows_coyote-{}-setup.exe".format(version))
    if not os.path.isfile(path):
        report.add("REVIEW", "releases/latest.json", "缺少更新清单")
        return
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.add("FAIL", "releases/latest.json", "无法解析", str(exc))
        return
    if data.get("version") != version:
        report.add("FAIL", "releases/latest.json", "版本不匹配",
                   str(data.get("version")))
    installer = data.get("installer")
    if not isinstance(installer, dict):
        report.add("FAIL", "releases/latest.json", "缺少 installer 块")
        return
    if os.path.isfile(setup):
        if installer.get("sha256") != _sha256(setup):
            report.add("FAIL", "releases/latest.json",
                       "installer.sha256 与实际安装包不一致")
        if installer.get("size") != os.path.getsize(setup):
            report.add("FAIL", "releases/latest.json",
                       "installer.size 与实际安装包不一致")
    urls = installer.get("urls")
    if not isinstance(urls, list) or not urls or not str(
            urls[0]).startswith("https://"):
        report.add("FAIL", "releases/latest.json", "installer.urls 不是 HTTPS")
    if not str(data.get("notes_url") or "").startswith("https://"):
        report.add("FAIL", "releases/latest.json", "notes_url 不是 HTTPS")


def _runtime_notes(report: Report) -> None:
    print("\n[工作区运行时产物]（不会被发布集收录，但请不要整目录打包）")
    for base in ("standalone", "wows_coyote"):
        root = os.path.join(ROOT, base)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if (name in ("qr.svg", "settings.json")
                        or name.endswith(".log")):
                    report.add("INFO", os.path.relpath(
                        os.path.join(dirpath, name), ROOT),
                        "运行时产物，发布集已排除")
            for name in list(dirnames):
                if name == "__pycache__":
                    report.add("INFO", os.path.relpath(
                        os.path.join(dirpath, name), ROOT),
                        "缓存目录，发布集已排除")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wows_coyote 发布审计")
    parser.add_argument("--no-archives", action="store_true",
                        help="只审计源码发布集")
    parser.add_argument("--archive", action="append", default=[],
                        help="额外指定要审计的 zip（可重复）")
    parser.add_argument("--strict", action="store_true",
                        help="REVIEW 也视为失败")
    parser.add_argument("--version", default=_project_version(),
                        help="要检查的安装包版本（默认取 __version__）")
    args = parser.parse_args(argv)

    report = Report(args.strict)
    _scan_source_set(report)

    if not args.no_archives:
        archives = list(args.archive)
        if not archives:
            releases = os.path.join(ROOT, "releases")
            if os.path.isdir(releases):
                archives = [
                    os.path.join(releases, name)
                    for name in sorted(os.listdir(releases))
                    if name.endswith(".zip")
                    and ("-gpl" in name or "-win64" in name)
                ]
        for path in archives:
            label = "source" if "-gpl" in os.path.basename(path) else "portable"
            _scan_zip(report, path, label)
        installer = os.path.join(
            ROOT, "dist_installer",
            "wows_coyote-{}-setup.exe".format(args.version))
        if os.path.isfile(installer):
            _scan_raw_file(report, installer)
        _check_sums(report)
        _check_manifest(report, args.version)

    _runtime_notes(report)

    print("\n" + "=" * 54)
    print("FAIL {}   REVIEW {}   INFO {}".format(
        report.fail, report.review, report.info))
    if report.ok():
        print("结论：通过（未发现需要阻断发布的内容）")
        return 0
    print("结论：未通过，请先处理上面的 FAIL/REVIEW 项")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
