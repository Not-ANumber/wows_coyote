# -*- coding: utf-8 -*-
"""Standalone 宿主入口：V3/V4 传输 + TelemetryReader + 调度器 + UI。

输出强度以 App/设备上报的通道上限为准；首次真机请先在 App 内把
通道上限调低，并在 App/设备侧确认输出。

用法：
  py -3.13 standalone/run.py                    # 默认 V4（官方 Relay）
  py -3.13 standalone/run.py --transport v3     # V3 Socket（郊狼 App）
  py -3.13 standalone/run.py --transport v4 --v4-relay local
  py -3.13 standalone/run.py --telemetry "D:\\...\\wows_telemetry.jsonl"
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from standalone import __version__, desktop
from standalone.config import load_config
from standalone.host import StandaloneHost
from standalone.hotkeys import VK_CODES, HotkeyWatcher
from standalone.official_templates import (
    EVENTS, TemplateLoadError, choose_defaults, format_catalog,
    load_from_manifest, load_from_sdk,
)
from standalone.plugin_bridge import ROOT as APP_ROOT
from standalone.telemetry import TelemetryRunner
from standalone.templates import TemplateLibrary, placeholder_library
from standalone.test_sequence import build_test_sequence, describe_sequence
from standalone.ui_server import UIBridge, UIServer
from standalone.update import (
    STARTUP_DELAY_S, UPDATE_MANIFEST_URL, UpdateChecker,
)
from standalone.v3_device import V3Device
from standalone.v4_device import (
    LOCAL_RELAY_PORT, V4Device, normalize_relay,
)

BUNDLED_MANIFEST = os.path.join(
    APP_ROOT, "standalone", "templates", "official_manifest.json")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="wows_coyote（V3 Socket / V4 Relay + 官方模板）")
    parser.add_argument("--transport", choices=("v3", "v4"), default="",
                        help="设备传输：v4（默认，DG-LAB 4）或 v3（郊狼 App）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=9999,
                        help="V3 监听端口，默认 9999")
    parser.add_argument("--v4-relay", default="",
                        help="V4 Relay：official（默认）/local/ws(s):// 地址")
    parser.add_argument("--v4-port", type=int, default=0,
                        help="本地 V4 中继端口，默认 {}".format(LOCAL_RELAY_PORT))
    parser.add_argument("--v4-slot", default="",
                        help="指定 V4 App 暴露的设备 slotId；默认自动选择郊狼设备")
    parser.add_argument("--advertise-ip", default="", help="二维码/URL 使用的局域网 IP")
    # 旧参数保留为兼容入口，避免已有快捷方式/脚本直接报错；实际不再限制输出。
    parser.add_argument("--max-strength", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--preset", default="", help="预设路径；默认用插件内置 v2 预设")
    parser.add_argument("--telemetry", default="", help="遥测文件/目录；默认按配置自动查找")
    parser.add_argument("--qr-path", default="", help="二维码输出路径")
    parser.add_argument("--no-telemetry", action="store_true",
                        help="不启动遥测读取，只测试设备配对/页面")
    parser.add_argument("--no-update-check", action="store_true",
                        help="本次启动不自动检查更新（设置页仍可手动检查）")
    parser.add_argument("--update-url", default="",
                        help="覆盖更新清单地址（联调/镜像）")
    parser.add_argument("--no-ui", action="store_true", help="不启动本地 UI 服务")
    parser.add_argument("--ui-port", type=int, default=0,
                        help="UI 端口，默认随机（127.0.0.1）")
    parser.add_argument("--window", action="store_true",
                        help="用 pywebview 桌面主窗口打开 UI（安装 pywebview 时默认）")
    parser.add_argument("--browser", action="store_true",
                        help="用浏览器打开 UI")
    parser.add_argument("--open-browser", action="store_true",
                        help="兼容旧参数：等同 --browser")
    parser.add_argument("--overlay", action="store_true",
                        help="打开置顶悬浮窗（窗口模式内嵌；浏览器模式用独立小窗）")
    parser.add_argument("--templates", default="bundled",
                        choices=("bundled", "sdk", "manifest", "placeholder", "none"),
                        help="模板来源：bundled（默认）/sdk/manifest/placeholder/none")
    parser.add_argument("--template-manifest", default="",
                        help="外部官方模板清单 JSON（--templates manifest）")
    parser.add_argument("--sdk-path", default="",
                        help="dglab-kit-python 源码目录或 coyote.py（--templates sdk）")
    parser.add_argument("--list-templates", action="store_true",
                        help="列出模板后退出，不启动设备/UI")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--log-file", default="", help="同时写入日志文件")
    parser.add_argument("--test-sequence", action="store_true",
                        help="App 绑定后自动跑 P3 固定事件序列（不需游戏）")
    parser.add_argument("--hotkey", default="F12",
                        help="Windows 全局急停键，默认 F12；可选 ESC/F1–F12")
    parser.add_argument("--no-hotkey", action="store_true",
                        help="禁用全局急停热键")
    parser.add_argument("--lock-hotkey", default="END",
                        help="Windows 全局悬浮窗锁定切换键，默认 END；"
                             "可选 ESC/F1–F12/PAUSE/HOME/END")
    parser.add_argument("--no-lock-hotkey", action="store_true",
                        help="禁用悬浮窗锁定切换热键")
    for event in EVENTS:
        parser.add_argument("--{}-template".format(event), default="",
                            help="覆盖 {} 事件的官方模板 id".format(event))
    return parser.parse_args(argv)


def load_templates(args: argparse.Namespace,
                   cfg: dict[str, Any]) -> tuple[TemplateLibrary, dict[str, str]]:
    source = args.templates
    if source in ("sdk", "bundled") and args.template_manifest:
        source = "manifest"
    if source == "placeholder":
        return placeholder_library(cfg), {}
    if source == "none":
        return TemplateLibrary("none"), {}
    if source == "manifest":
        if not args.template_manifest:
            raise TemplateLoadError("--templates manifest 需要 --template-manifest")
        library = load_from_manifest(args.template_manifest)
    elif source == "bundled":
        library = load_from_manifest(BUNDLED_MANIFEST)
    else:
        library = load_from_sdk(args.sdk_path or None)
    return library, choose_defaults(library)


def show_error(message: str) -> None:
    """冻结包无控制台时用系统弹窗报告启动失败。"""
    logging.error(message)
    if getattr(sys, "frozen", False) and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "wows_coyote", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.debug else logging.INFO
    handlers: list[logging.Handler] = []
    # 源码运行保留控制台；冻结包无控制台，默认写本机日志。
    # Windowed 构建里 sys.stderr 可能是 None：此时 --debug 再挂
    # StreamHandler 会让每条日志都抛一次 emit 异常，徒增开销。
    if not getattr(sys, "frozen", False) or (
            args.debug and sys.stderr is not None):
        handlers.append(logging.StreamHandler())
    log_file = args.log_file
    if not log_file and getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        log_file = os.path.join(base, "wows_coyote", "logs", "standalone.log")
    if log_file:
        try:
            directory = os.path.dirname(os.path.abspath(log_file))
            if directory:
                os.makedirs(directory, exist_ok=True)
            handlers.append(RotatingFileHandler(
                log_file, maxBytes=2 * 1024 * 1024, backupCount=2,
                encoding="utf-8"))
        except OSError as exc:
            print("日志文件不可写：{}".format(exc), file=sys.stderr)
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    if not args.debug:
        logging.getLogger("websockets").setLevel(logging.WARNING)


def spawn_overlay(ui_url: str) -> subprocess.Popen[Any] | None:
    """浏览器/无窗口模式：启动独立的置顶 overlay 进程。"""
    overlay_url = ui_url + "/overlay?app=1"
    if getattr(sys, "frozen", False):
        overlay_path = os.path.join(
            os.path.dirname(sys.executable), "wows_overlay.exe")
        if not os.path.isfile(overlay_path):
            print("未找到 wows_overlay.exe；请用主控制台悬浮窗按钮打开浏览器小窗")
            return None
        return subprocess.Popen([overlay_path, "--url", overlay_url])
    overlay_path = os.path.join(os.path.dirname(__file__), "overlay.py")
    return subprocess.Popen(
        [sys.executable, overlay_path, "--url", overlay_url])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.window and (args.browser or args.open_browser):
        print("--window 不能和 --browser/--open-browser 同时使用",
              file=sys.stderr)
        return 2
    if args.open_browser:
        args.browser = True
    setup_logging(args)
    cfg = load_config(args.preset or None)

    try:
        templates, mapping = load_templates(args, cfg)
    except TemplateLoadError as exc:
        print("官方模板加载失败：{}".format(exc), file=sys.stderr)
        if args.templates in ("bundled", "manifest") or args.list_templates:
            return 2
        print("fail-closed：当前只输出强度，不播放波形。", file=sys.stderr)
        templates, mapping = TemplateLibrary("none"), {}

    if args.list_templates:
        print(format_catalog(templates))
        return 0

    if args.templates in ("bundled", "sdk", "manifest"):
        if not len(templates):
            print("fail-closed：没有可用模板，只输出强度。", file=sys.stderr)
            for event in EVENTS:
                cfg["{}_preset".format(event)] = ""
        else:
            for event in EVENTS:
                key = "{}_preset".format(event)
                override = getattr(args, "{}_template".format(event), "")
                current = str(cfg.get(key, "") or "")
                if override:
                    choice = override
                elif current and templates.get(current) is not None:
                    # 用户设置里已有的有效模板优先，避免每次启动被默认映射覆盖。
                    choice = current
                elif current == "":
                    choice = ""
                else:
                    choice = mapping.get(event, "")
                if choice and templates.get(choice) is None:
                    print("模板不存在：{}（{}）".format(choice, event),
                          file=sys.stderr)
                    return 2
                cfg[key] = choice
            print("模板映射：" + "  ".join(
                "{}={}".format(event, cfg.get("{}_preset".format(event), "-") or "无")
                for event in EVENTS))
    elif args.templates == "none":
        for event in EVENTS:
            cfg["{}_preset".format(event)] = ""

    if args.max_strength is not None:
        print("提示：软件强度上限已取消，忽略 --max-strength；"
              "输出上限以 App/设备通道上限为准。")
    transport = str(args.transport or cfg.get("transport") or "v4").strip().lower()
    if transport not in ("v3", "v4"):
        print("不支持的传输方式：{}（可选 v3/v4）".format(transport),
              file=sys.stderr)
        return 2
    relay_value = args.v4_relay or str(cfg.get("v4_relay") or "official")
    if transport == "v4":
        try:
            normalize_relay(relay_value)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    slot_id = args.v4_slot or str(cfg.get("v4_slot") or "")

    def make_device(new_transport: str, new_relay: str) -> Any:
        if new_transport == "v4":
            return V4Device(
                relay=new_relay,
                host=args.host,
                port=args.v4_port or LOCAL_RELAY_PORT,
                advertise_ip=args.advertise_ip,
                qr_path=args.qr_path,
                slot_id=slot_id,
            )
        return V3Device(
            host=args.host,
            port=args.port,
            advertise_ip=args.advertise_ip,
            qr_path=args.qr_path,
        )

    try:
        device = make_device(transport, relay_value)
    except ValueError as exc:
        print("V4 配置错误：{}".format(exc), file=sys.stderr)
        return 2
    try:
        device.start()
    except Exception as exc:
        print("设备连接启动失败：{}".format(exc), file=sys.stderr)
        try:
            device.stop()
        except Exception:
            pass
        return 2

    devices: dict[str, Any] = {"current": device}
    active = {
        "transport": transport,
        "relay": relay_value if transport == "v4" else "",
    }
    switch_lock = threading.RLock()
    bridge = None

    def _safe_device_stop() -> None:
        try:
            devices["current"].stop()
        except Exception:
            logging.exception("退出时停止设备失败")

    atexit.register(_safe_device_stop)

    host = StandaloneHost(cfg, device, templates)

    def _install_device(new_device: Any, new_transport: str,
                        new_relay: str) -> None:
        host.sink = new_device
        host.engine.sink = new_device
        host.scheduler.sink = new_device
        # App 按钮急停时，同步清掉调度器 active；下次 tick 会重新按当前赢家播放。
        new_device.on_emergency = host.scheduler.clear_all
        # App 绑定/设备就绪后，让引擎重新下发当前强度；否则相同百分比的
        # 去重会让设备停在绑定时的 0。
        new_device.on_connected = host.engine.resend_strength
        devices["current"] = new_device
        active["transport"] = new_transport
        active["relay"] = new_relay if new_transport == "v4" else ""
        if bridge is not None:
            bridge.device = new_device

    _install_device(device, transport, active["relay"])

    def switch_transport(new_transport: str, new_relay: str) -> Any:
        new_transport = str(new_transport or "v4").strip().lower()
        new_relay = str(new_relay or "official").strip()
        if new_transport not in ("v3", "v4"):
            raise ValueError("不支持的连接方式：{}".format(new_transport))
        with switch_lock:
            old = devices["current"]
            old_transport = active["transport"]
            old_relay = active["relay"]
            try:
                host.scheduler.clear_all()
            except Exception:
                logging.exception("切换连接方式前清空调度失败")
            try:
                old.stop()
            except Exception:
                logging.exception("停止旧连接方式失败")
            try:
                new_device = make_device(new_transport, new_relay)
                new_device.start()
            except Exception as exc:
                # 新连接失败时尽量恢复旧连接，避免留在无设备状态。
                try:
                    restored = make_device(old_transport, old_relay)
                    restored.start()
                    _install_device(restored, old_transport, old_relay)
                except Exception:
                    logging.exception("恢复旧连接方式失败")
                raise RuntimeError("切换连接方式失败：{}".format(exc)) from exc
            _install_device(new_device, new_transport, new_relay)
            logging.info("连接方式已切换：%s / %s",
                         new_transport, new_relay or "-")
            return new_device

    hotkey_name = str(args.hotkey or "F12").upper()
    if not args.no_hotkey and hotkey_name not in VK_CODES:
        print("不支持的急停键：{}".format(hotkey_name), file=sys.stderr)
        devices["current"].stop()
        return 2
    lock_hotkey_name = str(args.lock_hotkey or "END").upper()
    lock_hotkey = (not args.no_lock_hotkey
                   and lock_hotkey_name not in ("", "NONE", "OFF"))
    if lock_hotkey and lock_hotkey_name not in VK_CODES:
        print("不支持的悬浮窗锁定键：{}".format(lock_hotkey_name),
              file=sys.stderr)
        devices["current"].stop()
        return 2
    if lock_hotkey and not args.no_hotkey and lock_hotkey_name == hotkey_name:
        print("悬浮窗锁定键不能和急停键相同：{}".format(lock_hotkey_name),
              file=sys.stderr)
        devices["current"].stop()
        return 2
    # UI bridge 在热键之后创建；用可变引用让锁定热键在 UI 起来后也能写配置。
    runtime: dict[str, Any] = {"bridge": None}
    hotkey = None
    bindings: dict[str, Any] = {}
    if not args.no_hotkey:
        def _global_estop() -> None:
            devices["current"].emergency_stop("全局急停 {}".format(hotkey_name))
            host.scheduler.clear_all()
            print("全局急停：{}".format(hotkey_name), flush=True)

        bindings[hotkey_name] = _global_estop
    if lock_hotkey:
        def _toggle_overlay_lock() -> None:
            bridge_ref = runtime["bridge"]
            config = bridge_ref.config if bridge_ref is not None else cfg
            value = not bool(config.get("overlay_locked", False))
            if bridge_ref is not None:
                try:
                    bridge_ref.update_settings({"overlay_locked": value})
                except Exception:
                    logging.exception("切换悬浮窗锁定失败")
                    return
            else:
                cfg["overlay_locked"] = value
            print("悬浮窗{}：{}".format(
                "已锁定（鼠标穿透）" if value else "已解锁",
                lock_hotkey_name), flush=True)

        bindings[lock_hotkey_name] = _toggle_overlay_lock
    if bindings:
        hotkey = HotkeyWatcher(bindings)
        hotkey.start()
    with switch_lock:
        host.tick(time.monotonic())

    runner = None
    if not args.test_sequence:
        runner = TelemetryRunner(
            host, args.telemetry or None, enabled=not args.no_telemetry)
    sequence_source = build_test_sequence() if args.test_sequence else None
    if sequence_source is not None:
        print(describe_sequence(sequence_source))

    stop_event = threading.Event()
    exit_shell: dict[str, Any] = {"shell": None}

    def zero_and_stop(reason: str) -> None:
        # 安全动作必须在任何打印/日志之前，避免 stdout 阻塞延误归零。
        try:
            devices["current"].emergency_stop(reason)
        except Exception:
            logging.exception("退出归零失败")
        try:
            host.scheduler.clear_all()
        except Exception:
            logging.exception("退出清空调度失败")
        stop_event.set()
        logging.info("退出程序：%s", reason)
        try:
            print("退出程序（{}）：已归零".format(reason), flush=True)
        except Exception:
            pass

    def request_exit(reason: str) -> None:
        shell_ref = exit_shell.get("shell")
        if shell_ref is not None:
            shell_ref.request_shutdown(reason)
        else:
            zero_and_stop(reason)

    def engine_loop() -> None:
        sequence = sequence_source
        sequence_started = False
        sequence_t0 = 0.0
        last_poll = 0.0
        while not stop_event.is_set():
            now = time.monotonic()
            if sequence is not None:
                if not sequence_started and devices["current"].connected:
                    sequence_started = True
                    sequence_t0 = now
                    print("P3 测试序列开始（App 已绑定）", flush=True)
                if sequence_started:
                    while sequence and now - sequence_t0 >= sequence[0][0]:
                        at, message = sequence.pop(0)
                        host.on_message(message)
                        print("  +{:>4.1f}s  {}".format(
                            at, message.get("type")), flush=True)
                    if not sequence:
                        print("P3 测试序列完成", flush=True)
                        sequence = None
            if runner is not None:
                try:
                    interval = float(host.config.get("polling_rate", 100)) / 1000.0
                except (TypeError, ValueError):
                    interval = 0.1
                interval = max(0.05, min(5.0, interval))
                if now - last_poll >= interval:
                    runner.poll()
                    last_poll = now
            with switch_lock:
                host.tick(now)
            stop_event.wait(0.05)

    update_checker = None
    if not args.no_ui:
        try:
            update_checker = UpdateChecker(
                __version__,
                manifest_url=(
                    args.update_url
                    or os.environ.get("WOWS_COYOTE_UPDATE_URL")
                    or UPDATE_MANIFEST_URL),
                enabled=(bool(cfg.get("check_updates", True))
                         and not args.no_update_check),
            )
        except Exception as exc:
            print("更新检查初始化失败：{}".format(exc), file=sys.stderr)

    ui = None
    bridge = None
    overlay_proc = None
    ui_url = ""
    ui_mode = "off"
    if not args.no_ui:
        bridge = UIBridge(host, device, runner, cfg, on_exit=request_exit,
                          on_transport_change=switch_transport,
                          update_checker=update_checker)
        runtime["bridge"] = bridge
        bridge.hotkeys = {
            "estop": "" if args.no_hotkey else hotkey_name,
            "overlay_lock": lock_hotkey_name if lock_hotkey else "",
        }
        bridge.sync_obs()
        try:
            ui = UIServer(bridge, port=args.ui_port)
            ui_url = ui.start()
        except OSError as exc:
            print("UI 启动失败（端口可能被占用）：{}".format(exc))
            ui = None
        if ui_url:
            if args.browser:
                ui_mode = "browser"
            elif args.window:
                if desktop.available():
                    ui_mode = "window"
                else:
                    print("未安装 pywebview，无法打开主窗口；改用浏览器模式。"
                          "安装：py -3.13 -m pip install pywebview")
                    ui_mode = "browser"
            elif desktop.available():
                ui_mode = "window"
            else:
                print("未安装 pywebview，使用浏览器模式。"
                      "安装：py -3.13 -m pip install pywebview")
                ui_mode = "browser"

        if ui_url and update_checker is not None and not args.test_sequence:
            def _delayed_update_check() -> None:
                if stop_event.wait(STARTUP_DELAY_S):
                    return
                update_checker.maybe_check_async()

            threading.Thread(
                target=_delayed_update_check,
                name="wows-update-check",
                daemon=True).start()

    print("=" * 72)
    print("wows_coyote v{} · {}".format(
        __version__, "V4 Relay" if transport == "v4" else "V3 Socket"))
    print("=" * 72)
    if transport == "v4":
        device_state = device.state()
        print("V4 中继：{}".format(
            "本机（端口 {}）".format(device_state.get("local_port"))
            if device_state.get("relay_mode") == "local"
            else device_state.get("relay_url", "-")))
        print("控制方 ID：{}".format(device.target_id or "—"))
        print("App 连接地址：")
        print("  " + (device.app_url or "—"))
        print("手机 App（DG-LAB 4）→ Socket 控制 → 扫码；"
              "V4 需要 App 4.x 与对应设备。")
    print("二维码内容：")
    print("  " + (device.qr_payload or "—"))
    print("二维码文件：")
    print("  " + device.qr_path)
    print("本机服务：")
    print("  " + device.ws_url)
    if ui_url:
        print("UI 模式：")
        print("  " + ("桌面主窗口" if ui_mode == "window"
                       else "浏览器 " + ui_url))
        print("主控制台：")
        print("  " + ui_url)
        print("设置页：")
        print("  " + ui_url + "/settings")
        print("Overlay：")
        print("  " + ui_url + "/overlay")
        if bridge is not None:
            obs_state = bridge.obs_state()
            print("OBS Browser Source：")
            if obs_state["running"]:
                print("  " + obs_state["url"])
            else:
                print("  未开启（设置页 → OBS 浏览器源）")
                if obs_state["error"]:
                    print("  错误：" + obs_state["error"])
    print("强度上限：以 App/设备通道上限为准（软件上限设置已取消）")
    print("模板源：{} · license={} · {} 条".format(
        templates.source, templates.license or "-", len(templates)))
    print("全局急停：{}".format(
        "已禁用" if args.no_hotkey else hotkey_name))
    print("悬浮窗锁定：{}".format(
        "已禁用" if not lock_hotkey
        else lock_hotkey_name + " 切换（鼠标点击穿透）"))
    if update_checker is not None:
        print("更新检查：{}".format(
            "启动后自动检查" if update_checker.enabled else "已关闭"))
    print("关闭主窗口：归零并退出；Ctrl+C 也可停止。")
    print("=" * 72)

    shell = None
    loop_thread = None
    try:
        if ui_mode == "window":
            shell = desktop.DesktopShell(
                ui_url, zero_and_stop,
                open_overlay=args.overlay, debug=args.debug,
                theme=str(cfg.get("theme") or "dark"))
            exit_shell["shell"] = shell
            loop_thread = threading.Thread(
                target=engine_loop, name="wows-engine", daemon=True)
            loop_thread.start()
            try:
                shell.run()
            except KeyboardInterrupt:
                print("\n收到 Ctrl+C，正在停止…")
                shell.request_shutdown("Ctrl+C")
            except Exception as exc:
                if shell is not None and shell.shutdown_requested:
                    # 用户已经关闭过窗口，退出阶段的库异常不再当成启动失败弹窗。
                    logging.warning("主窗口退出时异常（已归零）：%s", exc)
                    return 0
                logging.exception("主窗口启动失败")
                show_error(
                    "主窗口启动失败：{}\n\n"
                    "请安装 WebView2 Runtime，或使用 --browser 运行。".format(exc))
                return 3
        else:
            if ui_mode == "browser":
                webbrowser.open(ui_url)
                if args.overlay:
                    overlay_proc = spawn_overlay(ui_url)
            try:
                engine_loop()
            except KeyboardInterrupt:
                print("\n收到 Ctrl+C，正在停止…")
    finally:
        stop_event.set()
        if shell is not None:
            try:
                shell.request_shutdown("清理")
            except Exception:
                logging.exception("退出清理失败：关闭窗口")
        if loop_thread is not None:
            loop_thread.join(timeout=3.0)
        if hotkey is not None:
            try:
                hotkey.stop()
            except Exception:
                logging.exception("退出清理失败：停止热键")
        if overlay_proc is not None and overlay_proc.poll() is None:
            try:
                overlay_proc.terminate()
            except Exception:
                logging.exception("退出清理失败：结束 overlay")
        if update_checker is not None:
            try:
                update_checker.shutdown()
            except Exception:
                logging.exception("退出清理失败：停止更新检查")
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                logging.exception("退出清理失败：停止 OBS 服务")
        if ui is not None:
            try:
                ui.stop()
            except Exception:
                logging.exception("退出清理失败：停止 UI 服务")
        try:
            host.stop()
        except Exception:
            logging.exception("退出清理失败：停止宿主")
        try:
            devices["current"].stop()
        except Exception:
            logging.exception("退出清理失败：停止设备")
    logging.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
