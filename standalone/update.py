# -*- coding: utf-8 -*-
"""standalone 更新检查：只读清单 + 版本提示，不下载安装包。

设计约束：
  - 只接受 HTTPS 清单；本地 127.0.0.1/localhost 的 HTTP 仅用于测试；
  - 响应体上限 64 KB，超时默认 6 秒；
  - 24 小时内不重复自动检查；失败后 1 小时内不自动重试；
  - 只缓存检查结果，不缓存安装包，也不执行任何下载内容。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

LOG = logging.getLogger("standalone.update")

UPDATE_MANIFEST_URL = (
    "https://wowsdg-update.woyiba.com/wows_coyote/latest.json")
MAX_MANIFEST_BYTES = 64 * 1024
CHECK_INTERVAL_S = 24 * 60 * 60
FAIL_RETRY_S = 60 * 60
STARTUP_DELAY_S = 10
MAX_NOTES_CHARS = 8000
HTTP_TIMEOUT_S = 6.0
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class UpdateError(Exception):
    """清单不可用或格式错误；消息可以直接展示给用户。"""


@dataclass(frozen=True)
class Release:
    version: str
    published_at: str
    notes: str
    notes_url: str


def parse_version(text: Any) -> tuple[int, int, int]:
    match = _VERSION_RE.match(str(text or "").strip())
    if not match:
        raise ValueError("版本号必须是 X.Y.Z：{}".format(text))
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    try:
        return parse_version(latest) > parse_version(current)
    except ValueError:
        return False


def default_state_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "wows_coyote")


def _validate_manifest_url(url: str) -> str:
    text = str(url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme == "https" and parsed.netloc:
        return text
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return text
    raise ValueError("更新清单地址必须使用 HTTPS：{}".format(text or "(空)"))


def _validate_notes_url(url: str) -> str:
    text = str(url or "").strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("更新说明地址必须使用 HTTPS：{}".format(text or "(空)"))
    return text


def parse_manifest(raw: bytes) -> Release:
    """解析并校验 latest.json；未知字段忽略，坏数据整体拒绝。"""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新清单不是有效的 UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("更新清单根节点必须是 JSON 对象")
    if data.get("schema") != 1:
        raise UpdateError("不支持的更新清单版本")

    version = str(data.get("version") or "").strip()
    try:
        parse_version(version)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc

    notes = data.get("notes")
    if not isinstance(notes, str):
        notes = "" if notes is None else str(notes)
    notes = notes.strip()
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[:MAX_NOTES_CHARS] + "…"

    notes_url = str(data.get("notes_url") or "").strip()
    if not notes_url:
        raise UpdateError("更新清单缺少 notes_url")
    try:
        notes_url = _validate_notes_url(notes_url)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc

    published_at = data.get("published_at")
    if not isinstance(published_at, str):
        published_at = ""
    return Release(
        version=version,
        published_at=published_at.strip(),
        notes=notes,
        notes_url=notes_url,
    )


def _iso_utc(epoch: float) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(
        epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, (UpdateError, ValueError)):
        return str(exc)
    return "{}: {}".format(exc.__class__.__name__, exc)


class UpdateChecker:
    """后台检查一次清单，并把“是否有新版本”暴露给 UI。"""

    def __init__(
        self,
        current_version: str,
        manifest_url: str = UPDATE_MANIFEST_URL,
        state_dir: str | None = None,
        enabled: bool = True,
        fetch: Callable[[str, float], bytes] | None = None,
        clock: Callable[[], float] | None = None,
        timeout: float = HTTP_TIMEOUT_S,
        check_interval_s: float = CHECK_INTERVAL_S,
        fail_retry_s: float = FAIL_RETRY_S,
    ) -> None:
        self.current_version = str(current_version)
        self.manifest_url = _validate_manifest_url(manifest_url)
        self.state_dir = os.path.abspath(
            os.path.expanduser(state_dir or default_state_dir()))
        self.state_path = os.path.join(self.state_dir, "update_state.json")
        self.enabled = bool(enabled)
        self.timeout = max(1.0, float(timeout))
        self.check_interval_s = max(1.0, float(check_interval_s))
        self.fail_retry_s = max(1.0, float(fail_retry_s))
        self._fetch = fetch or self._http_fetch
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._checking = False
        self._release: Release | None = None
        self._error = ""
        self._checked_epoch = 0.0
        self._last_attempt_epoch = 0.0
        self._skipped_version = ""
        self._load_state()

    # ---------------------------------------------------------------- HTTP
    def _http_fetch(self, url: str, timeout: float) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "wows_coyote/{} (Windows)".format(
                    self.current_version),
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(MAX_MANIFEST_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise UpdateError("更新服务器返回 HTTP {}".format(exc.code)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise UpdateError("无法连接更新服务器：{}".format(reason)) from exc
        except (TimeoutError, OSError) as exc:
            raise UpdateError("连接更新服务器超时或失败：{}".format(exc)) from exc

    # ---------------------------------------------------------------- 状态
    def _load_state(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            self._checked_epoch = float(data.get("last_check_epoch") or 0)
            self._last_attempt_epoch = float(
                data.get("last_attempt_epoch") or 0)
        except (TypeError, ValueError):
            self._checked_epoch = 0.0
            self._last_attempt_epoch = 0.0
        self._skipped_version = str(data.get("skipped_version") or "").strip()
        release = data.get("release")
        if not isinstance(release, dict):
            return
        try:
            candidate = Release(
                version=str(release.get("version") or "").strip(),
                published_at=str(release.get("published_at") or "").strip(),
                notes=str(release.get("notes") or ""),
                notes_url=_validate_notes_url(
                    str(release.get("notes_url") or "").strip()),
            )
            parse_version(candidate.version)
        except (ValueError, UpdateError):
            return
        self._release = candidate

    def _save_state_locked(self) -> None:
        data = {
            "last_check_epoch": self._checked_epoch,
            "last_attempt_epoch": self._last_attempt_epoch,
            "skipped_version": self._skipped_version,
            "release": asdict(self._release) if self._release else None,
        }
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            LOG.warning("保存更新状态失败：%s", exc)

    def state(self) -> dict[str, Any]:
        with self._lock:
            release = self._release
            available = bool(
                release and is_newer(release.version, self.current_version))
            return {
                "enabled": self.enabled,
                "current": self.current_version,
                "checking": self._checking,
                "checked_at": _iso_utc(self._checked_epoch),
                "error": self._error,
                "available": available,
                "latest": release.version if release else "",
                "notes": release.notes if release else "",
                "notes_url": release.notes_url if release else "",
                "published_at": release.published_at if release else "",
                "skipped": bool(
                    release and self._skipped_version == release.version),
            }

    # ---------------------------------------------------------------- 检查
    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self.enabled = bool(value)

    def maybe_check_async(self) -> None:
        """启动后调用：只在启用且距上次成功/失败超过门槛时检查。"""
        with self._lock:
            if not self.enabled or self._checking:
                return
            now = self._clock()
            if (self._checked_epoch
                    and now - self._checked_epoch < self.check_interval_s):
                return
            if (self._last_attempt_epoch
                    and now - self._last_attempt_epoch < self.fail_retry_s):
                return
        self.check_async()

    def check_async(self, force: bool = False) -> None:
        """异步检查；force=True 时忽略 enabled/缓存（手动检查用）。"""
        with self._lock:
            if self._checking:
                return
            if not force and not self.enabled:
                return
            self._checking = True
            self._error = ""
        thread = threading.Thread(
            target=self._run_check, name="wows-update-fetch", daemon=True)
        thread.start()

    def _run_check(self) -> None:
        try:
            raw = self._fetch(self.manifest_url, self.timeout)
            if not isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise UpdateError("更新清单响应过大")
            release = parse_manifest(bytes(raw))
            with self._lock:
                self._release = release
                self._error = ""
                self._checked_epoch = self._clock()
                self._last_attempt_epoch = self._checked_epoch
                self._save_state_locked()
            LOG.info("更新检查完成：当前 %s，最新 %s",
                     self.current_version, release.version)
        except Exception as exc:
            message = _error_text(exc)
            with self._lock:
                self._error = message
                self._last_attempt_epoch = self._clock()
                self._save_state_locked()
            LOG.warning("更新检查失败：%s", message)
        finally:
            with self._lock:
                self._checking = False

    # ---------------------------------------------------------------- 动作
    def skip(self, version: str = "") -> None:
        with self._lock:
            if self._release is None:
                raise ValueError("还没有可跳过的版本")
            target = str(version or self._release.version).strip()
            if target != self._release.version:
                raise ValueError("版本已变化，请先重新检查更新")
            self._skipped_version = target
            self._save_state_locked()

    def unskip(self) -> None:
        with self._lock:
            if not self._skipped_version:
                return
            self._skipped_version = ""
            self._save_state_locked()

    def open_notes(self) -> bool:
        with self._lock:
            url = self._release.notes_url if self._release else ""
        if not url:
            return False
        try:
            _validate_notes_url(url)
            return bool(webbrowser.open(url))
        except Exception:
            LOG.exception("打开更新说明失败")
            return False

    def shutdown(self) -> None:
        """退出时停止后续自动检查；进行中的请求最多等到超时。"""
        with self._lock:
            self.enabled = False
