# -*- coding: utf-8 -*-
"""战舰世界 × 郊狼 v2 — DGHub 外部插件.

五类事件：基础（HP%）、受击（聚合掉血窗口）、起火、进水、死亡。
每通道强度相加并按 cap 截断；优先级只决定每通道播放哪个波形。
强度通过 set_strength 绝对写入，波形通过 trigger action=waveform 播放。
"""

import asyncio
import json
import logging
import logging.handlers
import math
import os
import re
import tempfile
import threading
import time
import urllib.request

try:
    import websockets
except Exception:  # 允许离线单测导入本模块
    websockets = None


LOG_FORMAT = "%(asctime)s - [%(levelname)s]: %(message)s"
LOG_FILE_NAME = 'wows_coyote.log'
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 2
WATCHDOG_TIMEOUT_S = 8.0

PLUGIN_ID = 'wows_coyote'
TELEMETRY_NAME = 'wows_telemetry.jsonl'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(PLUGIN_DIR, 'manifest.json')
BUNDLED_PRESET_PATH = os.path.join(PLUGIN_DIR, 'wows_preset_v2.json')
BASE_CURVE_PATH = os.path.join(PLUGIN_DIR, 'base.json')
HIT_CURVE_PATH = os.path.join(PLUGIN_DIR, 'hit.json')
STATUS_TITLE = '战舰世界 × 郊狼 启动检查'
V1_PRESET_PREFIXES = ('evt_', 'ui_', 'hard_', 'hp_env_', 'fire_env_', 'flood_env_')
PRESET_PATH_KEYS = ('preset_file_path', 'telemetry_path')
EVENT_PRIORITIES = {'base': 1, 'fire': 2, 'flood': 3, 'hit': 4, 'death': 5}
FIXED_CAP_PCT = 100
FIXED_HIT_WINDOW_S = 0.3
RETIRED_CONFIG_KEYS = (
    'hit_window_max_ms', 'hit_window_ms', 'mix_interval_ms', 'auto_detect',
    'cap_a', 'cap_b', 'base_curve_file',
    'base_priority', 'hit_priority', 'fire_priority',
    'flood_priority', 'death_priority',
    'fire_stack_waveform', 'flood_stack_waveform',
)

HEAD_LEN = 256
LOOP_WAVE_INTERVAL_S = 3.0      # 火/水状态期间内部重发波形的步长
TRIGGER_EVENT_MAX_S = 20.0     # 受击/死亡：强度与波形共用的可见上限
MAX_READ_BYTES = 256 * 1024    # 单次遥测读取上限，避免一次性阻塞事件循环
BASE_CURVE_MAX_BYTES = 256 * 1024
BASE_CURVE_MAX_POINTS = 64
BASE_CURVE_REFRESH_S = 2.0
MIX_INTERVAL_S = 0.1           # 内部混合节拍；不对外提供配置项
PROBE_INTERVAL_S = 2.0
AUTO_CACHE_SEC = 30.0
WAIT_LOG_INTERVAL_S = 30.0
EMPTY_SOURCE_SWITCH_S = 10.0

plugin_cfg = {}
send_queue = None

_RUNTIME_LOG_PATH = None
_watchdog_lock = threading.Lock()
_watchdog_active = False
_watchdog_last_beat = 0.0
_watchdog_thread = None


def _runtime_log_path():
    """返回可写的运行日志路径；DGHub 下绝不回退到 stdout。"""
    candidates = [os.path.join(PLUGIN_DIR, LOG_FILE_NAME)]
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        candidates.append(os.path.join(
            local_app_data, 'DGHub', 'logs', LOG_FILE_NAME))
    candidates.append(os.path.join(
        tempfile.gettempdir(), 'DGHub_wows_coyote.log'))
    for path in candidates:
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, 'a', encoding='utf-8'):
                pass
            return path
        except OSError:
            continue
    return None


def setup_logging():
    """DGHub spawn 时写滚动文件；只有手动运行时才用控制台。"""
    global _RUNTIME_LOG_PATH
    logger = logging.getLogger()
    if getattr(logger, '_wows_coyote_logging_configured', False):
        return
    logger.setLevel(logging.INFO)
    logger.handlers[:] = []
    logger.propagate = False
    # 日志 handler 自己出错时不能回退到 stderr，否则会再次阻塞管道。
    logging.raiseExceptions = False
    formatter = logging.Formatter(LOG_FORMAT)
    in_dghub = bool(os.environ.get('DGHUB_TOKEN')
                    or os.environ.get('DGHUB_PLUGIN_ID'))
    if in_dghub:
        path = _runtime_log_path()
        if path:
            try:
                handler = logging.handlers.RotatingFileHandler(
                    path,
                    maxBytes=LOG_MAX_BYTES,
                    backupCount=LOG_BACKUP_COUNT,
                    encoding='utf-8',
                    delay=True,
                )
                handler.setFormatter(formatter)
                logger.addHandler(handler)
                _RUNTIME_LOG_PATH = path
            except Exception:
                logger.addHandler(logging.NullHandler())
        else:
            logger.addHandler(logging.NullHandler())
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger._wows_coyote_logging_configured = True


def _watchdog_stale(now=None):
    now = time.monotonic() if now is None else now
    with _watchdog_lock:
        return bool(_watchdog_active and _watchdog_last_beat > 0
                    and now - _watchdog_last_beat > WATCHDOG_TIMEOUT_S)


def _watchdog_loop():
    while True:
        time.sleep(1.0)
        if _watchdog_stale():
            # 事件循环已停止处理消息；立即退出，避免旧进程占住下一次启动。
            os._exit(1)


def _start_watchdog():
    global _watchdog_thread
    if _watchdog_thread is not None:
        return
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name='wows_coyote_watchdog',
        daemon=True,
    )
    _watchdog_thread.start()


def _watchdog_activate():
    global _watchdog_active, _watchdog_last_beat
    with _watchdog_lock:
        _watchdog_active = True
        _watchdog_last_beat = time.monotonic()


def _watchdog_deactivate():
    global _watchdog_active
    with _watchdog_lock:
        _watchdog_active = False


def _watchdog_beat():
    global _watchdog_last_beat
    with _watchdog_lock:
        _watchdog_last_beat = time.monotonic()


# ----------------------------------------------------------------------
# 配置读取
# ----------------------------------------------------------------------

def _to_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def cfg_num(key, default=0.0):
    try:
        return float(plugin_cfg.get(key, default))
    except Exception:
        try:
            return float(default)
        except Exception:
            return 0.0


def cfg_int(key, default=0):
    try:
        return int(float(plugin_cfg.get(key, default)))
    except Exception:
        try:
            return int(default)
        except Exception:
            return 0


def cfg_bool(key, default=False):
    return _to_bool(plugin_cfg.get(key, default), default)


def cfg_str(key, default=''):
    value = plugin_cfg.get(key, default)
    return default if value is None else str(value)


def _clamp_num(value, low, high):
    try:
        value = float(value)
    except Exception:
        value = 0.0
    return max(low, min(high, value))


def _clamp_int(value, low, high):
    try:
        value = int(round(float(value)))
    except Exception:
        value = 0
    return max(low, min(high, value))


def _wave_duration(value):
    try:
        value = float(value)
    except Exception:
        value = 0.1
    return max(0.1, value)


def _trigger_duration(value, default=2.0):
    try:
        value = float(value)
    except Exception:
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(0.1, min(TRIGGER_EVENT_MAX_S, value))


def _as_number(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    return bool(value)


# ----------------------------------------------------------------------
# manifest / 预设导入导出
# ----------------------------------------------------------------------

def load_manifest():
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def _load_schema_meta():
    types = {}
    defaults = {}
    try:
        manifest = load_manifest()
        for section in manifest.get('config_schema', []):
            for field in section.get('fields', []):
                key = field.get('key')
                if not key:
                    continue
                types[key] = field.get('type', 'text')
                defaults[key] = field.get('default')
    except Exception as exc:
        logging.error('读取 manifest.json 失败: %s', exc)
    return types, defaults


SCHEMA_TYPES, SCHEMA_DEFAULTS = _load_schema_meta()
# 以 manifest 的默认值作为初始配置；DGHub 随后推送的 config 会覆盖它们。
plugin_cfg.update(SCHEMA_DEFAULTS)


def auto_cast(key, value):
    """按 manifest 的类型转换配置；null/非法数值回退到该字段默认值。"""
    default = SCHEMA_DEFAULTS.get(key)
    if value is None:
        return default
    field_type = SCHEMA_TYPES.get(key)
    try:
        if field_type == 'bool':
            return _to_bool(value, bool(default) if default is not None else False)
        if field_type == 'percent':
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('non-finite number')
            return _clamp_int(number, 0, 100)
        if field_type == 'number':
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('non-finite number')
            return int(number) if number.is_integer() else number
        if field_type == 'duration':
            number = float(value)
            if not math.isfinite(number):
                raise ValueError('non-finite number')
            if key in ('hit_duration_s', 'death_duration_s'):
                return _trigger_duration(number, default or 2.0)
            return number
        if field_type in ('text', 'path', 'preset', 'select'):
            return str(value)
    except Exception:
        return default if default is not None else value

    # manifest 不可用时的兜底，只处理最常见的键。
    lowered = str(value).strip().lower()
    if key.endswith('_enable') or key.endswith('_trigger') or key in (
            'trigger_import', 'trigger_export', 'debug_log',
            'hp_env_enable', 'hard_reset_on_start'):
        return lowered in ('1', 'true', 'yes', 'on')
    if key.endswith(('_dur', '_timeout_s')):
        try:
            return float(value)
        except Exception:
            return value
    if key.endswith(('_delta_a', '_delta_b', '_priority', '_add', '_sub',
                     '_step', '_max', '_threshold', '_hysteresis', '_pct',
                     '_damage', '_ms', '_rate', '_base_a', '_base_b')):
        try:
            return int(float(value))
        except Exception:
            return value
    return value


def _clean_path(raw):
    text = str(raw or '').strip().strip('"').strip("'")
    if not text:
        return ''
    text = os.path.expandvars(os.path.expanduser(text))
    return os.path.normpath(text)


def import_preset(filepath):
    path = _clean_path(filepath)
    if not path:
        dghub_log('warning', '请先填写预设文件')
        return False
    if not os.path.isfile(path):
        dghub_log('error', '预设文件不存在: {}'.format(path))
        return False
    try:
        with open(path, 'r', encoding='utf-8-sig') as handle:
            data = json.load(handle)
    except Exception as exc:
        dghub_log('error', '读取预设失败: {}'.format(exc))
        return False
    if not isinstance(data, dict):
        dghub_log('error', '预设格式错误：根节点必须是 JSON 对象')
        return False

    v1_keys = [key for key in data if key.startswith(V1_PRESET_PREFIXES)]
    if v1_keys:
        dghub_log(
            'error',
            '检测到 v1 预设（{} 个 v1 键，如 {}）；v2 事件模型不兼容，'
            '请改用 wows_preset_v2.json'.format(
                len(v1_keys), ', '.join(v1_keys[:3])))
        return False

    imported = 0
    ignored_paths = []
    for key, value in data.items():
        if key in ('trigger_import', 'trigger_export'):
            continue
        if key in PRESET_PATH_KEYS:
            ignored_paths.append(key)
            continue
        if SCHEMA_TYPES and key not in SCHEMA_TYPES:
            continue
        cast = auto_cast(key, value)
        plugin_cfg[key] = cast
        enqueue({'op': 'set_config', 'key': key, 'value': cast})
        imported += 1
    if ignored_paths:
        dghub_log('info', '已忽略预设中的本机路径: {}'.format(', '.join(ignored_paths)))
    dghub_log('info', '📥 导入完成 ({} 项)，请退出并重新进入设置页以刷新控件'.format(imported))
    return True


def export_preset(filepath):
    path = _clean_path(filepath)
    if not path:
        dghub_log('warning', '请先填写预设文件')
        return False

    data = {}
    for key, default in SCHEMA_DEFAULTS.items():
        if key in ('trigger_import', 'trigger_export') or key in PRESET_PATH_KEYS:
            continue
        data[key] = plugin_cfg.get(key, default)
    for key, value in plugin_cfg.items():
        if key in ('trigger_import', 'trigger_export') or key in PRESET_PATH_KEYS or key in data:
            continue
        data[key] = value

    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=4))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        dghub_log('error', '导出预设失败: {}'.format(exc))
        return False
    dghub_log('info', '📤 导出成功: {}'.format(path))
    return True


def ensure_preset_path():
    if cfg_str('preset_file_path', '').strip():
        return
    path = BUNDLED_PRESET_PATH
    if not os.path.isfile(path):
        return
    plugin_cfg['preset_file_path'] = path
    enqueue({'op': 'set_config', 'key': 'preset_file_path', 'value': path})
    logging.info('预设文件为空，已填入插件内置预设: %s', path)


def schedule_toggle_reset(key):
    async def _reset():
        await asyncio.sleep(1.5)
        plugin_cfg[key] = False
        enqueue({'op': 'set_config', 'key': key, 'value': False})

    try:
        asyncio.create_task(_reset())
    except Exception:
        pass


# ----------------------------------------------------------------------
# DGHub 消息 / 状态上报
# ----------------------------------------------------------------------

def enqueue(message):
    if send_queue is None:
        return
    try:
        send_queue.put_nowait(message)
    except Exception as exc:
        logging.error('消息入队失败: %s', exc)


def dghub_log(level, message):
    log_fn = {
        'debug': logging.debug,
        'info': logging.info,
        'warning': logging.warning,
        'error': logging.error,
    }.get(level, logging.info)
    log_fn(message)
    enqueue({'op': 'log', 'level': level, 'message': str(message)})


_status_steps = {
    'plugin': {
        'key': 'plugin',
        'title': '启动插件',
        'state': 'ok',
        'detail': '插件进程已连接 DGHub',
    },
    'telemetry': {
        'key': 'telemetry',
        'title': '遥测数据源',
        'state': 'pending',
        'detail': '等待 ExportShipStatus 输出',
        'hint': '进入一场战斗后会自动读取',
    },
    'battle': {
        'key': 'battle',
        'title': '战局',
        'state': 'idle',
        'detail': '等待进入战斗',
    },
}
_status_display = '等待遥测文件'
_status_sig = None


def reset_status_cache():
    global _status_sig
    _status_sig = None


def update_status(display=None, step=None, state=None, detail=None, hint=None):
    global _status_display, _status_sig

    if display is not None:
        _status_display = display
    if step:
        item = _status_steps.get(step)
        if item is not None:
            if state is not None:
                item['state'] = state
            if detail is not None:
                item['detail'] = detail
            if hint is not None:
                item['hint'] = hint
            elif state is not None:
                item.pop('hint', None)

    steps = []
    for key in ('plugin', 'telemetry', 'battle'):
        item = dict(_status_steps[key])
        if not item.get('hint'):
            item.pop('hint', None)
        steps.append(item)

    fields = {
        'display_status': _status_display,
        'startup_check': {'title': STATUS_TITLE, 'steps': steps},
    }
    signature = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    if signature == _status_sig:
        return
    _status_sig = signature
    enqueue({'op': 'status', 'fields': fields})


# ----------------------------------------------------------------------
# 遥测路径发现
# ----------------------------------------------------------------------

_auto_cache_at = 0.0
_auto_cache = []


def _resolve_telemetry_path(raw):
    path = _clean_path(raw)
    if not path:
        return ''
    if not os.path.isdir(path):
        return path
    direct = os.path.join(path, TELEMETRY_NAME)
    if os.path.isfile(direct):
        return direct
    profile = os.path.join(path, 'profile', TELEMETRY_NAME)
    if os.path.isfile(profile):
        return profile
    if os.path.basename(path).lower() == 'profile':
        return direct
    if os.path.isdir(os.path.join(path, 'profile')):
        return profile
    parts = path.split(os.sep)
    if any(part.lower() == 'bin' for part in parts[1:]):
        return _profile_path_from_root(path)
    return direct


def _configured_telemetry_path():
    return _resolve_telemetry_path(cfg_str('telemetry_path', ''))


def _telemetry_path_is_valid(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, 'rb') as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def _profile_path_from_root(root):
    root = _clean_path(root)
    if not root:
        return ''
    if os.path.isfile(root):
        if os.path.basename(root).lower() == TELEMETRY_NAME:
            return root
        root = os.path.dirname(root)
    if os.path.basename(root).lower() == 'profile':
        return os.path.join(root, TELEMETRY_NAME)
    parts = root.split(os.sep)
    for index, part in enumerate(parts):
        if part.lower() == 'bin' and index > 0:
            root = os.sep.join(parts[:index])
            break
    return os.path.join(root, 'profile', TELEMETRY_NAME)


def _steam_library_roots():
    roots = set()
    steam_dirs = [
        r'C:\Program Files (x86)\Steam',
        r'C:\Program Files\Steam',
    ]
    for letter in 'CDEFGH':
        steam_dirs.append('{}:\\Steam'.format(letter))
        steam_dirs.append('{}:\\SteamLibrary'.format(letter))
    for steam_dir in steam_dirs:
        vdf = os.path.join(steam_dir, 'steamapps', 'libraryfolders.vdf')
        try:
            with open(vdf, 'r', encoding='utf-8', errors='replace') as handle:
                text = handle.read(65536)
        except Exception:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            roots.add(match.group(1).replace('\\\\', '\\'))
    return roots


def _wgc_roots():
    roots = []
    try:
        import winreg
    except Exception:
        return roots

    keys = [
        (winreg.HKEY_CURRENT_USER, r'Software\Wargaming.net\GameCenter\wows'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Wargaming.net\GameCenter\wows'),
        (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Wargaming.net\GameCenter\wows'),
    ]
    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in ('path', 'game_path', 'install_path', 'Path'):
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if value:
                        roots.append(str(value))
        except OSError:
            continue
    return roots


def _auto_candidates():
    global _auto_cache_at, _auto_cache

    now = time.monotonic()
    if _auto_cache and now - _auto_cache_at < AUTO_CACHE_SEC:
        return list(_auto_cache)

    roots = []
    for env_name in ('WOWS_DIR', 'WOWS_ROOT', 'WORLD_OF_WARSHIPS_DIR'):
        value = os.environ.get(env_name)
        if value:
            roots.append(value)

    drives = []
    for letter in 'CDEFGH':
        drive = '{}:\\'.format(letter)
        try:
            if os.path.exists(drive):
                drives.append(drive)
        except Exception:
            pass

    suffixes = [
        'World_of_Warships',
        'World of Warships',
        os.path.join('Games', 'World_of_Warships'),
        os.path.join('Games', 'World of Warships'),
        os.path.join('Games', 'SteamLibrary', 'steamapps', 'common', 'World of Warships'),
        os.path.join('SteamLibrary', 'steamapps', 'common', 'World of Warships'),
        os.path.join('SteamLibrary', 'steamapps', 'common', 'World of Warships Public Test'),
        os.path.join('Steam', 'steamapps', 'common', 'World of Warships'),
        os.path.join('Program Files', 'World of Warships'),
        os.path.join('Program Files (x86)', 'World of Warships'),
        os.path.join('Wargaming.net', 'World_of_Warships'),
    ]
    for drive in drives:
        for suffix in suffixes:
            roots.append(os.path.join(drive, suffix))

    for library in _steam_library_roots():
        roots.append(os.path.join(library, 'steamapps', 'common', 'World of Warships'))
        roots.append(os.path.join(library, 'steamapps', 'common', 'World of Warships Public Test'))
    roots.extend(_wgc_roots())

    paths = []
    seen = set()
    for root in roots:
        candidate = _profile_path_from_root(root)
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(candidate)

    _auto_cache = paths
    _auto_cache_at = now
    return list(paths)


def find_telemetry_path():
    manual = _configured_telemetry_path()
    if _telemetry_path_is_valid(manual):
        return manual

    existing = []
    for candidate in _auto_candidates():
        if not _telemetry_path_is_valid(candidate):
            continue
        try:
            # 自动发现只接受已经有内容的文件。导出插件会在开战前先创建
            # 0 字节文件，若此时选中一个残留的空文件，读取器会一直盯着
            # 它，之后另一个真正写入的文件永远不会被尝试。
            if os.path.getsize(candidate) <= 0:
                continue
        except OSError:
            continue
        existing.append(candidate)
    if not existing:
        return None
    if len(existing) == 1:
        return existing[0]
    try:
        existing.sort(key=lambda item: os.path.getmtime(item), reverse=True)
    except Exception:
        pass
    return existing[0]


def _newest_written_auto_candidate(exclude=None):
    """在自动候选里找最新且有内容的文件，用于空文件兜底切换。"""
    excluded = os.path.normcase(os.path.normpath(exclude)) if exclude else None
    best = None
    best_mtime = None
    for candidate in _auto_candidates():
        if excluded is not None and \
                os.path.normcase(os.path.normpath(candidate)) == excluded:
            continue
        if not _telemetry_path_is_valid(candidate):
            continue
        try:
            if os.path.getsize(candidate) <= 0:
                continue
            mtime = os.path.getmtime(candidate)
        except OSError:
            continue
        if best is None or best_mtime is None or mtime > best_mtime:
            best = candidate
            best_mtime = mtime
    return best


def _load_curve_file(path):
    """读取并校验强度 JSON 函数表，返回按横坐标降序的点。"""
    with open(path, 'rb') as handle:
        raw = handle.read(BASE_CURVE_MAX_BYTES + 1)
    if len(raw) > BASE_CURVE_MAX_BYTES:
        raise ValueError('文件超过 256KB')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('不是 UTF-8 文件: {}'.format(exc))
    try:
        data = json.loads(text)
    except Exception as exc:
        raise ValueError('JSON 解析失败: {}'.format(exc))
    if not isinstance(data, dict):
        raise ValueError('根节点必须是 JSON 对象')
    if 'version' in data and data.get('version') != 1:
        raise ValueError('version 只支持 1')
    points = data.get('points')
    if not isinstance(points, list):
        raise ValueError('缺少 points 数组')
    if not 2 <= len(points) <= BASE_CURVE_MAX_POINTS:
        raise ValueError('points 数量必须在 2–{}'.format(BASE_CURVE_MAX_POINTS))

    parsed = []
    seen = set()
    for index, item in enumerate(points, 1):
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError('第 {} 个点必须是 [横坐标, 强度]'.format(index))
        x_value, strength = item
        if isinstance(x_value, bool) or not isinstance(x_value, (int, float)):
            raise ValueError('第 {} 个点横坐标不是数字'.format(index))
        if isinstance(strength, bool) or not isinstance(strength, (int, float)):
            raise ValueError('第 {} 个点强度不是数字'.format(index))
        x_value = float(x_value)
        strength = float(strength)
        if not math.isfinite(x_value) or not math.isfinite(strength):
            raise ValueError('第 {} 个点包含非有限数值'.format(index))
        if not 0.0 <= x_value <= 100.0:
            raise ValueError('第 {} 个点横坐标必须在 0–100'.format(index))
        if not 0.0 <= strength <= 100.0:
            raise ValueError('第 {} 个点强度必须在 0–100'.format(index))
        key = round(x_value, 6)
        if key in seen:
            raise ValueError('重复横坐标: {}'.format(x_value))
        seen.add(key)
        parsed.append((x_value, strength))

    parsed.sort(key=lambda point: point[0], reverse=True)
    if abs(parsed[0][0] - 100.0) > 1e-6 or abs(parsed[-1][0]) > 1e-6:
        raise ValueError('points 必须包含 100 和 0 两个端点')
    return parsed


def _load_base_curve(path):
    return _load_curve_file(path)


class CurveTable(object):
    """固定路径的强度函数表：校验、热加载和线性插值。"""

    def __init__(self, path, label):
        self.path = path
        self.label = label
        self.points = None
        self.mtime = None
        self.last_check = 0.0
        self.error = ''

    def set_path(self, path):
        if path == self.path:
            return
        self.path = path
        self.points = None
        self.mtime = None
        self.last_check = 0.0
        self.error = ''

    def log_error(self, message):
        if message != self.error:
            self.error = message
            logging.warning('%s函数表: %s', self.label, message)

    def refresh(self, now):
        if not self.path:
            return
        if now - self.last_check < BASE_CURVE_REFRESH_S:
            return
        self.last_check = now
        try:
            stat = os.stat(self.path)
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            self.log_error('无法读取 {}: {}'.format(self.path, exc))
            return
        if self.points is not None and signature == self.mtime:
            return
        try:
            points = _load_curve_file(self.path)
        except Exception as exc:
            self.mtime = signature
            self.log_error(str(exc))
            return
        self.points = points
        self.mtime = signature
        self.error = ''
        logging.info('%s函数表已加载: %s (%d 点)',
                     self.label, self.path, len(points))

    def value(self, x_value):
        points = self.points
        if not points:
            return None
        x_value = _clamp_num(x_value, 0.0, 100.0)
        if x_value >= points[0][0]:
            return _clamp_num(points[0][1], 0.0, 100.0)
        if x_value <= points[-1][0]:
            return _clamp_num(points[-1][1], 0.0, 100.0)
        for (high_x, high_value), (low_x, low_value) in zip(points, points[1:]):
            if low_x <= x_value <= high_x:
                if high_x == low_x:
                    return _clamp_num(high_value, 0.0, 100.0)
                value = high_value + (low_value - high_value) * (
                    (high_x - x_value) / (high_x - low_x))
                return _clamp_num(value, 0.0, 100.0)
        return 0.0


# ----------------------------------------------------------------------
# 事件引擎
# ----------------------------------------------------------------------

class MixerEngine(object):
    """v2 事件引擎：每通道强度相加，按 cap 截断；优先级只仲裁波形。

    强度通过 DGHub set_strength 绝对写入；波形通过 trigger action=waveform
    单独播放，不携带强度目标。
    """

    def __init__(self, now_fn=None, base_curve_path=None, hit_curve_path=None):
        self._now = now_fn or time.monotonic
        self.base_curve_path = base_curve_path or BASE_CURVE_PATH
        self.hit_curve_path = hit_curve_path or HIT_CURVE_PATH
        self._base_curve = CurveTable(self.base_curve_path, '基础')
        self._hit_curve = CurveTable(self.hit_curve_path, '受击')

        self.battle_active = False
        self.state_ready = False
        self.dead = False
        self.alive = True
        self.max_hp = 0
        self.hp_pct = None
        self.burning = False
        self.flooding = False
        self.fires = 0
        self.floods = 0
        self.last_msg_time = 0.0
        self.battle_started_at = 0.0

        self._last_sent = {'a': None, 'b': None}
        self._last_totals = {'a': 0, 'b': 0}
        self._last_contributions = {}

        self._hit_value = 0.0
        self._hit_until = 0.0
        self._hit_started_at = 0.0
        self._hit_retrigger = False
        self._hit_window_open = False
        self._hit_window_start = 0.0
        self._hit_window_total = 0.0

        self._death_value = 0.0
        self._death_until = 0.0
        self._death_at = 0.0

        self._fire_started_at = 0.0
        self._flood_started_at = 0.0

        self._wave_sig = {'a': None, 'b': None}
        self._wave_winner = {'a': None, 'b': None}
        self._base_next = {'a': 0.0, 'b': 0.0}
        self._wave_loop_next = {'a': 0.0, 'b': 0.0}
        self._seed_waveforms = False
        self._last_tick = 0.0

    # ------------------------------------------------------------------
    # DGHub 输出
    # ------------------------------------------------------------------

    def _send_strength(self, value_a, value_b, force=False):
        value_a = _clamp_int(value_a, 0, 100)
        value_b = _clamp_int(value_b, 0, 100)
        if (not force and self._last_sent['a'] == value_a
                and self._last_sent['b'] == value_b):
            return
        if value_a == value_b:
            enqueue({'op': 'set_strength', 'channel': 'both', 'pct': value_a})
        else:
            enqueue({'op': 'set_strength', 'channel': 'a', 'pct': value_a})
            enqueue({'op': 'set_strength', 'channel': 'b', 'pct': value_b})
        self._last_sent = {'a': value_a, 'b': value_b}
        logging.debug('set_strength A=%d B=%d', value_a, value_b)

    def _play_waveform(self, event_id, channel, spec):
        duration = _wave_duration(spec['duration'])
        enqueue({
            'op': 'trigger',
            'action': 'waveform',
            'delta_pct': 0,
            'strength_mode': 'rollback',
            'duration_s': round(duration, 3),
            'preset': spec['preset'],
            'channel': channel,
            'label': spec['label'],
        })
        logging.info('🎵 波形 %s → %s (%.1fs, %s)',
                     spec['label'], channel, duration, spec['preset'])

    def startup_reset(self):
        # 清掉 v1 可能留下的 permanent baseline；之后强度只走 set_strength。
        enqueue({
            'op': 'trigger',
            'action': 'strength',
            'strength_mode': 'permanent',
            'delta_pct': -100,
            'channel': 'both',
            'label': '插件启动基线归零',
        })
        self._last_sent = {'a': None, 'b': None}
        self._send_strength(0, 0, force=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _reset_runtime(self):
        self.state_ready = False
        self.dead = False
        self.alive = True
        self.max_hp = 0
        self.hp_pct = None
        self.burning = False
        self.flooding = False
        self.fires = 0
        self.floods = 0

        self._hit_value = 0.0
        self._hit_until = 0.0
        self._hit_started_at = 0.0
        self._hit_retrigger = False
        self._hit_window_open = False
        self._hit_window_start = 0.0
        self._hit_window_total = 0.0

        self._death_value = 0.0
        self._death_until = 0.0
        self._death_at = 0.0

        self._fire_started_at = 0.0
        self._flood_started_at = 0.0

        self._wave_sig = {'a': None, 'b': None}
        self._wave_winner = {'a': None, 'b': None}
        self._base_next = {'a': 0.0, 'b': 0.0}
        self._wave_loop_next = {'a': 0.0, 'b': 0.0}
        self._seed_waveforms = False
        self._last_totals = {'a': 0, 'b': 0}
        self._last_contributions = {}

    def begin_battle(self, ship_desc='', adopt=False):
        if self.battle_active:
            logging.info('检测到新战局（上一局未收到 battle_end）')
        self._send_strength(0, 0)
        self._reset_runtime()
        now = self._now()
        self.battle_active = True
        self.battle_started_at = now
        self.last_msg_time = now
        self._seed_waveforms = bool(adopt)

        if adopt:
            logging.info('检测到进行中的战斗，从当前状态接入')
            update_status('战斗中', step='battle', state='ok',
                          detail='已接入进行中的战斗')
        else:
            update_status('战斗中', step='battle', state='ok',
                          detail=ship_desc or '已进入战斗')
            dghub_log('info', '🎮 战局开始' + (' · ' + ship_desc if ship_desc else ''))

    def end_battle(self, reason='', notify=True):
        # notify 仅为兼容 v1 reader 的调用签名；v2 没有结束事件。
        if not self.battle_active:
            if self._last_sent != {'a': 0, 'b': 0}:
                self._send_strength(0, 0)
            return
        self._send_strength(0, 0)
        self._reset_runtime()
        self.battle_active = False
        update_status('战局结束，等待下一场', step='battle', state='idle',
                      detail='等待进入战斗')
        logging.info('战局结束 (%s)', reason)
        dghub_log('info', '🏁 战局结束')

    def on_stream_reset(self, reason):
        self.end_battle(reason, notify=False)

    # ------------------------------------------------------------------
    # 消息入口
    # ------------------------------------------------------------------

    def on_message(self, message):
        if not isinstance(message, dict):
            return
        message_type = message.get('type')
        now = self._now()
        self.last_msg_time = now

        if message_type == 'battle_start':
            ship = message.get('ship_name')
            ship_type = message.get('ship_type')
            parts = [str(item) for item in (ship, ship_type) if item]
            self.begin_battle(ship_desc=' / '.join(parts), adopt=False)
            return

        if not self.battle_active:
            if message_type in ('snapshot', 'status_change', 'count_change',
                                'damage_taken'):
                self.begin_battle(adopt=True)
            else:
                return

        if message_type == 'snapshot':
            self._on_snapshot(message, now)
        elif message_type == 'status_change':
            self._on_status_change(message, now)
        elif message_type == 'count_change':
            self._on_count_change(message, now)
        elif message_type == 'damage_taken':
            self._on_damage(message, now)
        elif message_type == 'battle_end':
            self.end_battle('收到 battle_end')
        elif cfg_bool('debug_log', False):
            logging.info('[遥测] 未知消息类型: %s', message_type)

    def _on_snapshot(self, message, now):
        max_hp = _as_number(message.get('max_health'), 0) or 0
        health = _as_number(message.get('health'), 0) or 0
        hp_pct = _as_number(message.get('hp_pct'), None)
        alive = _as_bool(message.get('alive'), True)
        burning = _as_bool(message.get('burning'), False)
        flooding = _as_bool(message.get('flooding'), False)
        fires = int(_as_number(message.get('fires'), 0) or 0)
        floods = int(_as_number(message.get('floods'), 0) or 0)

        if max_hp > 0:
            self.max_hp = int(round(max_hp))
        if hp_pct is None and max_hp > 0:
            hp_pct = 100.0 * health / max_hp
        if hp_pct is not None:
            self.hp_pct = _clamp_num(hp_pct, 0.0, 100.0)

        self._observe_status(alive, burning, flooding, fires, floods, now)
        if not self.alive:
            self.hp_pct = 0.0

    def _on_status_change(self, message, now):
        kind = message.get('kind')
        state = message.get('state')
        count = _as_number(message.get('count'), None)
        if kind == 'alive' and state == 'ended':
            self._observe_status(alive=False, now=now)
        elif kind == 'burning':
            active = state == 'started'
            if active:
                self._observe_status(burning=True,
                                     fires=(int(count) if count is not None else None),
                                     now=now)
            else:
                self._observe_status(burning=False, fires=0, now=now)
        elif kind == 'flooding':
            active = state == 'started'
            if active:
                self._observe_status(flooding=True,
                                     floods=(int(count) if count is not None else None),
                                     now=now)
            else:
                self._observe_status(flooding=False, floods=0, now=now)

    def _on_count_change(self, message, now):
        kind = message.get('kind')
        count = int(_as_number(message.get('count'), 0) or 0)
        if kind == 'burning':
            self._observe_status(burning=(count > 0), fires=count, now=now)
        elif kind == 'flooding':
            self._observe_status(flooding=(count > 0), floods=count, now=now)

    def _hit_window_seconds(self):
        return FIXED_HIT_WINDOW_S

    def _on_damage(self, message, now):
        source = message.get('source')
        if source != 'damages':
            if cfg_bool('debug_log', False):
                logging.info('[遥测] 忽略非聚合伤害流: %s', source)
            return
        if not self.alive or self.dead:
            return
        amount = _as_number(message.get('amount'), 0) or 0
        if amount <= 0:
            return

        window = self._hit_window_seconds()
        # 固定窗口：从第一条伤害开始计时，到点即结算，不因后续伤害延长。
        if self._hit_window_open and now - self._hit_window_start >= window:
            self._evaluate_hit_window(now)
        if not self._hit_window_open:
            self._hit_window_open = True
            self._hit_window_start = now
            self._hit_window_total = 0.0
        self._hit_window_total += amount

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def _observe_status(self, alive=None, burning=None, flooding=None,
                        fires=None, floods=None, now=None):
        now = self._now() if now is None else now

        if not self.state_ready:
            if alive is not None:
                self.alive = bool(alive)
            if burning is not None:
                self.burning = bool(burning)
            if flooding is not None:
                self.flooding = bool(flooding)
            if fires is not None:
                self.fires = max(0, int(fires))
            elif burning is False:
                self.fires = 0
            if floods is not None:
                self.floods = max(0, int(floods))
            elif flooding is False:
                self.floods = 0
            if self.burning:
                self.fires = max(1, self.fires)
                self._fire_started_at = now
            if self.flooding:
                self.floods = max(1, self.floods)
                self._flood_started_at = now
            if not self.alive:
                self.dead = True
                self.alive = False
                self._hit_value = 0.0
                self._hit_until = 0.0
                self._hit_window_open = False
                self._hit_window_total = 0.0
                self._hit_window_start = 0.0
                self.burning = False
                self.flooding = False
                self.fires = 0
                self.floods = 0
                self._death_value = 0.0
                self._death_until = 0.0
            self.state_ready = True
            return

        if self.dead:
            self.burning = False
            self.flooding = False
            self.fires = 0
            self.floods = 0
            return

        if alive is not None:
            if self.alive and not alive:
                self._trigger_death(now)
                return
            if alive:
                self.alive = True

        if burning is not None:
            new_active = bool(burning)
            new_count = self.fires if fires is None else max(0, int(fires))
            if new_active:
                new_count = max(1, new_count)
            else:
                new_count = 0
            if new_active and not self.burning:
                self.burning = True
                self.fires = new_count
                self._fire_started_at = now
            elif new_active and self.burning:
                self.fires = new_count
            else:
                self.burning = False
                self.fires = 0

        if flooding is not None:
            new_active = bool(flooding)
            new_count = self.floods if floods is None else max(0, int(floods))
            if new_active:
                new_count = max(1, new_count)
            else:
                new_count = 0
            if new_active and not self.flooding:
                self.flooding = True
                self.floods = new_count
                self._flood_started_at = now
            elif new_active and self.flooding:
                self.floods = new_count
            else:
                self.flooding = False
                self.floods = 0

    def _evaluate_hit_window(self, now):
        if not self._hit_window_open:
            return
        total = self._hit_window_total
        if total <= 0 or self.dead or not self.alive:
            self._hit_window_open = False
            self._hit_window_total = 0.0
            self._hit_window_start = 0.0
            return
        if not cfg_bool('hit_enable', True):
            self._hit_window_open = False
            self._hit_window_total = 0.0
            self._hit_window_start = 0.0
            return
        if self.max_hp <= 0:
            # 首帧快照前先保留窗口，等 max_health 到达后再结算。
            return
        self._hit_window_open = False
        self._hit_window_total = 0.0
        self._hit_window_start = 0.0

        loss_pct = 100.0 * total / self.max_hp
        threshold = max(0.0, cfg_num('hit_threshold_pct', 2))
        if loss_pct < threshold:
            return

        scaled = None
        if cfg_bool('hit_curve_enable', False):
            scaled = self._hit_curve.value(loss_pct)
            if scaled is not None and scaled <= 0:
                return
        if scaled is None:
            full = max(threshold, cfg_num('hit_full_pct', 8))
            if full <= 0:
                full = max(0.01, threshold)
            strength = cfg_num('hit_strength', 40)
            scaled = _clamp_num(
                strength * min(1.0, loss_pct / full), 0.0, 100.0)
        if scaled <= 0:
            # 0 强度受击不建立事件，也不触发波形。
            return
        if self._hit_until > now:
            self._hit_value = max(self._hit_value, scaled)
        else:
            self._hit_value = scaled
        self._hit_started_at = now
        duration = _trigger_duration(cfg_num('hit_duration_s', 2.0), 2.0)
        self._hit_until = now + duration
        self._hit_retrigger = True
        logging.info('受击窗口: %.0f 伤害 = %.2f%% 最大血量 → 强度 %.1f%%',
                     total, loss_pct, scaled)

    def _trigger_death(self, now):
        if self.dead:
            return
        self.dead = True
        self.alive = False
        self.hp_pct = 0.0
        self.burning = False
        self.flooding = False
        self.fires = 0
        self.floods = 0
        self._hit_value = 0.0
        self._hit_until = 0.0
        self._hit_window_open = False
        self._hit_window_total = 0.0
        self._hit_window_start = 0.0
        self._death_at = now
        self._death_value = (cfg_num('death_strength', 60)
                             if cfg_bool('death_enable', True) else 0.0)
        self._death_until = now + _trigger_duration(
            cfg_num('death_duration_s', 4.0), 4.0)
        update_status('已阵亡，等待战局结束', step='battle', state='warn',
                      detail='阵亡')
        dghub_log('info', '💀 阵亡')
        logging.info('阵亡')

    # ------------------------------------------------------------------
    # 混合计算
    # ------------------------------------------------------------------

    @staticmethod
    def _binding_includes(binding, channel):
        binding = str(binding or 'both').strip().lower()
        if binding in ('both', 'a+b', 'ab'):
            return True
        return binding == channel

    def _refresh_curves(self, now):
        self._base_curve.set_path(self.base_curve_path)
        self._hit_curve.set_path(self.hit_curve_path)
        if cfg_bool('base_curve_enable', False):
            self._base_curve.refresh(now)
        if cfg_bool('hit_curve_enable', False):
            self._hit_curve.refresh(now)

    def _base_strength(self):
        hp = self.hp_pct
        if hp is None:
            return 0.0
        if cfg_bool('base_curve_enable', False):
            value = self._base_curve.value(hp)
            if value is not None:
                return value
        base_min = cfg_num('base_min', 10)
        base_max = cfg_num('base_max', 40)
        return _clamp_num(
            base_min + (base_max - base_min) * (1.0 - hp / 100.0),
            0.0, 100.0)

    def _event_values(self, now):
        if not self.battle_active:
            return {'base': 0.0, 'hit': 0.0, 'fire': 0.0,
                    'flood': 0.0, 'death': 0.0}

        base = 0.0
        if (not self.dead and self.hp_pct is not None
                and cfg_bool('base_enable', True)):
            base = self._base_strength()

        hit = 0.0
        if (not self.dead and self._hit_until > now
                and cfg_bool('hit_enable', True)):
            hit = _clamp_num(self._hit_value, 0.0, 100.0)

        fire = 0.0
        if (not self.dead and self.burning
                and cfg_bool('fire_enable', True)):
            count = max(1, int(self.fires))
            fire = (cfg_num('fire_strength', 12)
                    + (count - 1) * cfg_num('fire_stack_add', 6))
            fire = max(0.0, fire)

        flood = 0.0
        if (not self.dead and self.flooding
                and cfg_bool('flood_enable', True)):
            count = max(1, int(self.floods))
            flood = (cfg_num('flood_strength', 12)
                     + (count - 1) * cfg_num('flood_stack_add', 6))
            flood = max(0.0, flood)

        death = 0.0
        if (self.dead and self._death_until > now
                and cfg_bool('death_enable', True)):
            death = _clamp_num(self._death_value, 0.0, 100.0)

        return {'base': base, 'hit': hit, 'fire': fire,
                'flood': flood, 'death': death}

    def _channel_totals(self, now):
        values = self._event_values(now)
        channels = {
            'base': cfg_str('base_channel', 'both'),
            'hit': cfg_str('hit_channel', 'both'),
            'fire': cfg_str('fire_channel', 'both'),
            'flood': cfg_str('flood_channel', 'both'),
            'death': cfg_str('death_channel', 'both'),
        }
        caps = {'a': FIXED_CAP_PCT, 'b': FIXED_CAP_PCT}
        contributions = {}
        totals = {'a': 0.0, 'b': 0.0}
        for event_id, value in values.items():
            binding = channels[event_id]
            item = {'value': value, 'a': 0.0, 'b': 0.0,
                    'channel': str(binding or 'both')}
            for ch in ('a', 'b'):
                if self._binding_includes(binding, ch):
                    item[ch] = value
                    totals[ch] += value
            contributions[event_id] = item
        totals['a'] = min(float(caps['a']), totals['a'])
        totals['b'] = min(float(caps['b']), totals['b'])
        self._last_contributions = contributions
        self._last_totals = totals
        return contributions, totals, caps

    # ------------------------------------------------------------------
    # 波形仲裁
    # ------------------------------------------------------------------

    def _wave_spec(self, event_id, now, values=None):
        # 统一门限：当前贡献强度为 0 的事件不参与波形仲裁。
        if values is None:
            values = self._event_values(now)
        if float(values.get(event_id) or 0.0) <= 0.0:
            return None
        if event_id == 'base':
            if (not self.battle_active or self.dead or self.hp_pct is None
                    or not cfg_bool('base_enable', True)):
                return None
            preset = cfg_str('base_preset', '')
            if not preset:
                return None
            return {
                'event': 'base',
                'priority': EVENT_PRIORITIES['base'],
                'start': self.battle_started_at,
                'preset': preset,
                'duration': LOOP_WAVE_INTERVAL_S,
                'channel': cfg_str('base_channel', 'both'),
                'label': '基础',
            }
        if event_id == 'hit':
            if (not self.battle_active or self.dead or self._hit_until <= now
                    or not cfg_bool('hit_enable', True)):
                return None
            preset = cfg_str('hit_preset', '')
            if not preset:
                return None
            return {
                'event': 'hit',
                'priority': EVENT_PRIORITIES['hit'],
                'start': self._hit_started_at,
                'preset': preset,
                'duration': _trigger_duration(
                    cfg_num('hit_duration_s', 2.0), 2.0),
                'channel': cfg_str('hit_channel', 'both'),
                'label': '受击',
            }
        if event_id == 'fire':
            if (not self.battle_active or self.dead or not self.burning
                    or not cfg_bool('fire_enable', True)):
                return None
            preset = cfg_str('fire_preset', '')
            if not preset:
                return None
            return {
                'event': 'fire',
                'priority': EVENT_PRIORITIES['fire'],
                'start': self._fire_started_at,
                'preset': preset,
                'duration': LOOP_WAVE_INTERVAL_S,
                'channel': cfg_str('fire_channel', 'both'),
                'label': '起火 x{}'.format(max(1, int(self.fires))),
            }
        if event_id == 'flood':
            if (not self.battle_active or self.dead or not self.flooding
                    or not cfg_bool('flood_enable', True)):
                return None
            preset = cfg_str('flood_preset', '')
            if not preset:
                return None
            return {
                'event': 'flood',
                'priority': EVENT_PRIORITIES['flood'],
                'start': self._flood_started_at,
                'preset': preset,
                'duration': LOOP_WAVE_INTERVAL_S,
                'channel': cfg_str('flood_channel', 'both'),
                'label': '进水 x{}'.format(max(1, int(self.floods))),
            }
        if event_id == 'death':
            if (not self.battle_active or not self.dead or self._death_until <= now
                    or not cfg_bool('death_enable', True)):
                return None
            preset = cfg_str('death_preset', '')
            if not preset:
                return None
            return {
                'event': 'death',
                'priority': EVENT_PRIORITIES['death'],
                'start': self._death_at,
                'preset': preset,
                'duration': _trigger_duration(
                    cfg_num('death_duration_s', 4.0), 4.0),
                'channel': cfg_str('death_channel', 'both'),
                'label': '死亡',
            }
        return None

    def _pick_wave(self, channel, now):
        best = None
        values = self._event_values(now)
        for event_id in ('base', 'hit', 'fire', 'flood', 'death'):
            spec = self._wave_spec(event_id, now, values)
            if spec is None:
                continue
            if not self._binding_includes(spec['channel'], channel):
                continue
            key = (spec['priority'], spec['start'])
            if best is None or key > best[0]:
                best = (key, spec)
        return best[1] if best is not None else None

    def _arbitrate_waveforms(self, now):
        if self._seed_waveforms:
            if not self.state_ready:
                return
            for channel in ('a', 'b'):
                spec = self._pick_wave(channel, now)
                if spec is None:
                    self._wave_sig[channel] = None
                    self._wave_winner[channel] = None
                    self._base_next[channel] = 0.0
                    self._wave_loop_next[channel] = 0.0
                    continue
                self._wave_sig[channel] = (spec['event'], spec['preset'],
                                           round(spec['duration'], 3))
                self._wave_winner[channel] = spec['event']
                if spec['event'] == 'base':
                    self._base_next[channel] = now + LOOP_WAVE_INTERVAL_S
                    self._wave_loop_next[channel] = 0.0
                elif spec['event'] in ('fire', 'flood'):
                    # 接入中局：不立刻补波形，但按状态在 3s 后进入循环。
                    self._base_next[channel] = 0.0
                    self._wave_loop_next[channel] = now + LOOP_WAVE_INTERVAL_S
                elif spec['event'] in ('hit', 'death'):
                    # 一次性波形：中途接入不补历史，也不进入循环。
                    self._base_next[channel] = 0.0
                    self._wave_loop_next[channel] = 0.0
                else:
                    self._base_next[channel] = 0.0
                    self._wave_loop_next[channel] = 0.0
            self._seed_waveforms = False
            self._hit_retrigger = False
            return

        for channel in ('a', 'b'):
            spec = self._pick_wave(channel, now)
            if spec is None:
                if self._wave_sig[channel] is not None:
                    self._wave_sig[channel] = None
                    self._wave_winner[channel] = None
                self._base_next[channel] = 0.0
                self._wave_loop_next[channel] = 0.0
                continue

            sig = (spec['event'], spec['preset'],
                   round(spec['duration'], 3))
            previous = self._wave_winner[channel]

            if spec['event'] == 'base':
                self._wave_loop_next[channel] = 0.0
                if previous != 'base':
                    self._wave_sig[channel] = sig
                    self._wave_winner[channel] = 'base'
                    self._play_waveform('base', channel, spec)
                    self._base_next[channel] = now + LOOP_WAVE_INTERVAL_S
                    continue
                self._wave_sig[channel] = sig
                if self._base_next[channel] <= 0:
                    self._base_next[channel] = now + LOOP_WAVE_INTERVAL_S
                elif now >= self._base_next[channel]:
                    self._play_waveform('base', channel, spec)
                    self._base_next[channel] = now + LOOP_WAVE_INTERVAL_S
                continue

            self._base_next[channel] = 0.0
            event_id = spec['event']
            loop_event = event_id in ('fire', 'flood')
            loop_interval = LOOP_WAVE_INTERVAL_S

            if previous != event_id or self._wave_sig[channel] != sig:
                self._wave_sig[channel] = sig
                self._wave_winner[channel] = event_id
                self._play_waveform(event_id, channel, spec)
                self._wave_loop_next[channel] = (
                    now + loop_interval if loop_event else 0.0)
            elif event_id == 'hit' and self._hit_retrigger:
                self._play_waveform('hit', channel, spec)
                self._wave_loop_next[channel] = 0.0
            elif loop_event:
                if self._wave_loop_next[channel] <= 0:
                    self._wave_loop_next[channel] = now + loop_interval
                elif now >= self._wave_loop_next[channel]:
                    self._play_waveform(event_id, channel, spec)
                    self._wave_loop_next[channel] = now + loop_interval
            else:
                self._wave_loop_next[channel] = 0.0

        self._hit_retrigger = False

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def tick(self, now=None):
        now = self._now() if now is None else now
        self._refresh_curves(now)
        if not self.battle_active:
            if self._last_sent != {'a': 0, 'b': 0}:
                self._send_strength(0, 0)
            return

        timeout = max(5.0, cfg_num('telemetry_timeout_s', 60))
        if self.last_msg_time and now - self.last_msg_time > timeout:
            logging.warning('遥测超时 %.0f 秒，自动清零本局', now - self.last_msg_time)
            self.end_battle('遥测超时')
            return

        window = self._hit_window_seconds()
        if (self._hit_window_open
                and now - self._hit_window_start >= window):
            self._evaluate_hit_window(now)

        if self._hit_until and now >= self._hit_until:
            self._hit_value = 0.0
            self._hit_until = 0.0
        if self._death_until and now >= self._death_until:
            self._death_value = 0.0
            self._death_until = 0.0

        _, totals, _ = self._channel_totals(now)
        self._send_strength(totals['a'], totals['b'])
        self._arbitrate_waveforms(now)
        self._last_tick = now

    # ------------------------------------------------------------------
    # 供模拟台/调试读取
    # ------------------------------------------------------------------

    def export_state(self):
        now = self._now()
        contributions, totals, caps = self._channel_totals(now)
        state = {
            'battle_active': bool(self.battle_active),
            'alive': bool(self.alive),
            'dead': bool(self.dead),
            'state_ready': bool(self.state_ready),
            'hp_pct': round(self.hp_pct, 1) if self.hp_pct is not None else None,
            'max_hp': self.max_hp,
            'burning': bool(self.burning),
            'fires': int(self.fires),
            'flooding': bool(self.flooding),
            'floods': int(self.floods),
            'contributions': contributions,
            'totals': {'a': round(totals['a'], 1), 'b': round(totals['b'], 1)},
            'caps': caps,
            'wave': {'a': self._wave_winner.get('a'),
                     'b': self._wave_winner.get('b')},
            'base_next': {'a': self._base_next.get('a', 0.0),
                          'b': self._base_next.get('b', 0.0)},
            'wave_loop_next': {'a': self._wave_loop_next.get('a', 0.0),
                               'b': self._wave_loop_next.get('b', 0.0)},
            'hit_remaining': max(0.0, round(self._hit_until - now, 2)),
            'death_remaining': max(0.0, round(self._death_until - now, 2)),
            'hit_window_open': bool(self._hit_window_open),
            'last_sent': dict(self._last_sent),
        }
        return state


# ----------------------------------------------------------------------
# 遥测文件读取
# ----------------------------------------------------------------------

class TelemetryReader(object):
    """增量读取 ExportShipStatus 的 JSONL，并识别截断/重写/替换。"""

    def __init__(self, engine):
        self.engine = engine
        self.path = None
        self.file = None
        self.offset = 0
        self.buffer = b''
        self.head = b''
        self.ident = None
        self.cfg_key = None
        self.last_probe = 0.0
        self.last_wait_log = 0.0
        self.wait_started = time.monotonic()
        self.last_io_error = 0.0
        self.io_errors = 0
        self.malformed = 0
        self.last_malformed_log = 0.0
        self.found_once = False
        self.stream_started = False
        self.last_status_log = 0.0
        self.last_empty_switch = 0.0

    # ---- 数据源 --------------------------------------------------

    def poll(self):
        now = time.monotonic()
        if not self._ensure_source(now):
            self._log_waiting(now)
            return

        try:
            stat = os.stat(self.path)
        except OSError as exc:
            self._io_error('读取遥测文件状态失败', exc, now)
            return

        if now - self.last_status_log >= 30.0:
            self.last_status_log = now
            logging.debug('遥测状态: %s 大小=%d 已读=%d 已写入=%s 战局=%s',
                          self.path, stat.st_size, self.offset,
                          self.stream_started, self.engine.battle_active)

        identity = (getattr(stat, 'st_dev', 0), getattr(stat, 'st_ino', 0))
        if self.ident is not None and identity != self.ident:
            path = self.path
            logging.warning('遥测文件已被替换，重新接入')
            self._disconnect('文件被替换', notify=True)
            self._open_source(path, skip_history=True)
            return

        reset = False
        try:
            self.file.seek(0)
            head = self.file.read(HEAD_LEN)
            self.file.seek(self.offset)
        except Exception as exc:
            self._io_error('读取遥测文件头失败', exc, now)
            return

        if self.head:
            head_len = len(self.head)
            if len(head) < head_len or head[:head_len] != self.head:
                reset = True
        self.head = head

        if stat.st_size < self.offset:
            reset = True

        if reset:
            logging.info('检测到遥测文件截断/重写，重置到文件开头')
            self.engine.on_stream_reset('文件截断/重写')
            self.offset = 0
            self.buffer = b''

        if stat.st_size <= self.offset:
            self.io_errors = 0
            return

        try:
            self.file.seek(self.offset)
            read_size = min(stat.st_size - self.offset, MAX_READ_BYTES)
            chunk = self.file.read(read_size)
        except Exception as exc:
            self._io_error('读取遥测数据失败', exc, now)
            return
        if not chunk:
            self.io_errors = 0
            return

        if not self.stream_started:
            self.stream_started = True
            logging.info('📡 遥测数据开始写入: %s（%d 字节）',
                         self.path, stat.st_size)

        self.io_errors = 0
        self.offset += len(chunk)
        self.buffer += chunk
        if len(self.buffer) > 1024 * 1024:
            logging.warning('遥测缓冲区异常增大，丢弃未完成的行')
            self.buffer = b''
        self._parse_lines()

    def _ensure_source(self, now):
        key = _configured_telemetry_path()
        if key != self.cfg_key:
            if self.cfg_key is not None:
                logging.info('遥测数据源配置变化，重新连接')
                self.engine.end_battle('数据源配置变更', notify=False)
                self._close_handle()
                self.path = None
            self.cfg_key = key
            self.last_probe = 0.0

        if self.file is not None:
            self._switch_empty_source(now)
            return self.file is not None
        if now - self.last_probe < PROBE_INTERVAL_S:
            return False
        self.last_probe = now

        path = find_telemetry_path()
        if not path:
            return False
        configured = _configured_telemetry_path()
        if not _telemetry_path_is_valid(configured):
            self._adopt_auto_path(path)
            self.cfg_key = path
        return self._open_source(path, skip_history=True)

    def _switch_empty_source(self, now):
        """已连接的文件一直为空时，切换到另一个正在写入的自动候选。

        旧版本可能把 Steam 目录下的 0 字节残留文件自动填进设置；如果游戏
        实际写入的是另一个安装目录，只靠启动时的一次搜索会永远错过。
        """
        if self.stream_started:
            return
        if now - self.last_empty_switch < EMPTY_SOURCE_SWITCH_S:
            return
        self.last_empty_switch = now
        try:
            if os.path.getsize(self.path) > 0:
                return
        except OSError:
            return
        candidate = _newest_written_auto_candidate(exclude=self.path)
        if not candidate:
            return
        logging.warning(
            '当前遥测文件长时间没有数据，切换到正在写入的候选: %s -> %s',
            self.path, candidate)
        self.engine.end_battle('切换遥测数据源', notify=False)
        self._close_handle()
        self.path = None
        self._adopt_auto_path(candidate)
        self.cfg_key = candidate
        self.last_probe = 0.0
        self._open_source(candidate, skip_history=True)

    def _adopt_auto_path(self, path):
        current = _clean_path(cfg_str('telemetry_path', ''))
        if current == path:
            return
        plugin_cfg['telemetry_path'] = path
        enqueue({'op': 'set_config', 'key': 'telemetry_path', 'value': path})
        logging.info('📡 自动找到遥测文件，已填入设置: %s', path)

    @staticmethod
    def _last_complete_offset(handle, size):
        """返回最后一个换行之后的位置，避免从半行开始读取。"""
        if size <= 0:
            return 0
        look = min(size, 64 * 1024)
        handle.seek(size - look)
        tail = handle.read(look)
        index = tail.rfind(b'\n')
        if index < 0:
            return size
        return size - look + index + 1

    def _open_source(self, path, skip_history=True):
        handle = None
        try:
            handle = open(path, 'rb')
            stat = os.stat(path)
            size = stat.st_size
            handle.seek(0)
            head = handle.read(HEAD_LEN)
            self.file = handle
            self.path = path
            self.ident = (getattr(stat, 'st_dev', 0), getattr(stat, 'st_ino', 0))
            self.head = head
            self.stream_started = False
            self.last_status_log = 0.0
            self.last_empty_switch = 0.0
            if skip_history and size > 0:
                self.offset = self._last_complete_offset(handle, size)
                self.buffer = b''
                logging.info('📡 接入遥测文件: %s（从 %d 字节处接入）', path, self.offset)
            else:
                self.offset = 0
                self.buffer = b''
                logging.info('📡 接入遥测文件: %s（%d 字节）', path, size)
            self.found_once = True
            self.wait_started = time.monotonic()
            display = None if self.engine.battle_active else '遥测已连接，等待战斗'
            update_status(display, step='telemetry', state='ok',
                          detail='已连接: {}'.format(path))
            return True
        except Exception as exc:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
            logging.error('打开遥测文件失败 %s: %s', path, exc)
            self.file = None
            self.path = None
            self.ident = None
            self.head = b''
            self.offset = 0
            self.buffer = b''
            return False

    def _close_handle(self):
        if self.file is not None:
            try:
                self.file.close()
            except Exception:
                pass
        self.file = None
        self.offset = 0
        self.buffer = b''
        self.head = b''
        self.ident = None
        self.stream_started = False
        self.last_status_log = 0.0
        self.last_empty_switch = 0.0

    def _disconnect(self, reason, notify=True):
        logging.warning('遥测数据源断开: %s', reason)
        self.engine.end_battle(reason, notify=notify)
        self._close_handle()
        self.path = None
        self.io_errors = 0
        self.found_once = False
        self.wait_started = time.monotonic()
        update_status('遥测断开，等待重新连接', step='telemetry', state='warn',
                      detail=reason, hint='确认游戏仍在运行，或在设置中检查遥测路径')

    def _log_waiting(self, now):
        if now - self.last_wait_log >= WAIT_LOG_INTERVAL_S:
            self.last_wait_log = now
            logging.info('📡 等待 ExportShipStatus 遥测数据...')
        if now - self.wait_started > 10 and not self.found_once:
            update_status('等待遥测文件', step='telemetry', state='warn',
                          detail='等待 {} 写入'.format(TELEMETRY_NAME),
                          hint='开战前文件可能为 0 字节；若战斗中仍无数据，'
                               '请检查设置页遥测路径')
        else:
            update_status('等待遥测文件', step='telemetry', state='pending',
                          detail='等待 ExportShipStatus 写入',
                          hint='进入一场战斗后会自动读取')

    def _io_error(self, message, exc, now):
        self.io_errors += 1
        if self.io_errors <= 3 or now - self.last_io_error > 10:
            logging.warning('%s: %s', message, exc)
            self.last_io_error = now
        if self.io_errors >= 5:
            self._disconnect('连续读取失败')

    # ---- 行解析 --------------------------------------------------

    def _parse_lines(self):
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line.decode('utf-8-sig', 'replace'))
            except Exception:
                self._log_malformed(line)
                continue
            if not isinstance(message, dict):
                self._log_malformed(line)
                continue
            if cfg_bool('debug_log', False):
                _log_telemetry_message(message)
            try:
                self.engine.on_message(message)
            except Exception as exc:
                logging.error('处理遥测消息失败: %s', exc)

    def _log_malformed(self, line):
        self.malformed += 1
        now = time.monotonic()
        if now - self.last_malformed_log >= 10:
            self.last_malformed_log = now
            logging.warning('无法解析遥测行（累计 %d）: %r', self.malformed, line[:200])


def _log_telemetry_message(message):
    message_type = message.get('type')
    if message_type == 'snapshot':
        logging.info('[遥测] snapshot hp=%s/%s (%s%%) alive=%s 火=%s 水=%s',
                     message.get('health'), message.get('max_health'),
                     message.get('hp_pct'), message.get('alive'),
                     message.get('fires'), message.get('floods'))
        return
    try:
        text = json.dumps(message, ensure_ascii=False)
    except Exception:
        text = str(message)
    logging.info('[遥测] %s', text[:400])


# ----------------------------------------------------------------------
# 后台任务 / DGHub 协议
# ----------------------------------------------------------------------

async def telemetry_task(reader):
    logging.info('🛰️ 遥测监听已启动')
    while True:
        try:
            reader.poll()
            _watchdog_beat()
        except Exception as exc:
            logging.error('遥测轮询异常: %s', exc)
        delay = _clamp_num(cfg_num('polling_rate', 100) / 1000.0, 0.05, 5.0)
        await asyncio.sleep(delay)


async def mixer_task(engine):
    logging.info('🎚️ 强度混合器已启动')
    while True:
        try:
            engine.tick()
            _watchdog_beat()
        except Exception as exc:
            logging.error('混合器异常: %s', exc)
        await asyncio.sleep(MIX_INTERVAL_S)


def get_latest_token(host, port):
    try:
        url = 'http://{}:{}/api/plugins/_session_token'.format(host, port)
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(request, timeout=1.0) as response:
            raw = response.read().decode('utf-8', 'replace')
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get('token'):
                return str(data['token'])
        except Exception:
            pass
        return raw.strip().strip('"')
    except Exception:
        return None


async def try_connect(host, port, manifest_data):
    token = get_latest_token(host, port)
    if not token:
        return None
    uri = 'ws://{}:{}/ws/plugin?token={}'.format(host, port, token)
    try:
        async with websockets.connect(uri, close_timeout=1) as websocket:
            await websocket.send(json.dumps({
                'op': 'hello',
                'token': token,
                'manifest': manifest_data,
            }, ensure_ascii=False))
            raw = await asyncio.wait_for(websocket.recv(), timeout=2.0)
            ack = json.loads(raw)
            if ack.get('op') == 'hello_ack' and ack.get('accepted'):
                return token
            if ack.get('reason') == 'unknown_plugin_id':
                logging.error('DGHub 未注册此插件，请先在 插件中心→外部插件 导入 wows_coyote 的 zip')
                raise SystemExit(1)
            logging.warning('DGHub 握手被拒绝: %s', ack.get('reason'))
    except SystemExit:
        raise
    except Exception as exc:
        logging.debug('探测端口 %s 失败: %s', port, exc)
    return None


async def _send_shutdown_zero(websocket):
    message = {
        'op': 'set_strength',
        'channel': 'both',
        'pct': 0,
    }
    try:
        await websocket.send(json.dumps(message, ensure_ascii=False))
        await asyncio.sleep(0.1)
    except Exception as exc:
        # DGHub 可能先关闭 WebSocket 再处理 stop；此时归零已无意义。
        logging.info('停止时连接已关闭，跳过强度归零: %s', exc)


async def rx_loop(websocket):
    async for raw in websocket:
        try:
            message = json.loads(raw)
        except Exception:
            continue
        if not isinstance(message, dict):
            continue

        _watchdog_beat()
        op = message.get('op')
        if op == 'stop':
            logging.info('收到 DGHub 停止指令')
            await _send_shutdown_zero(websocket)
            return 'stop'

        if op == 'config':
            values = message.get('data') or {}
            if isinstance(values, dict):
                values = dict(values)
                # 2.0.4 起窗口固定从第一条伤害计时，不再使用“最长窗口”配置。
                # 2.0.5 起混合节拍固定为内部 100ms，不再接受外部配置。
                for retired in RETIRED_CONFIG_KEYS:
                    values.pop(retired, None)
                for key, value in values.items():
                    plugin_cfg[key] = auto_cast(key, value)
                logging.info('✅ 配置已加载 (%d 项)', len(values))
                ensure_preset_path()

        elif op == 'config_changed':
            key = message.get('key')
            if not key or key in RETIRED_CONFIG_KEYS:
                continue
            cast = auto_cast(key, message.get('value'))
            plugin_cfg[key] = cast
            logging.info('配置更新: %s = %r', key, cast)
            if key == 'trigger_import' and _to_bool(cast):
                schedule_toggle_reset('trigger_import')
                import_preset(cfg_str('preset_file_path', ''))
            elif key == 'trigger_export' and _to_bool(cast):
                schedule_toggle_reset('trigger_export')
                export_preset(cfg_str('preset_file_path', ''))

        elif op == 'device_info':
            if cfg_bool('debug_log', False):
                logging.info('设备信息: %s', message)

        elif op == 'ping':
            # 应用层 ping 不需要响应，websockets 协议层会自动保活。
            pass

    return 'closed'


async def tx_loop(websocket):
    while True:
        message = await send_queue.get()
        try:
            await websocket.send(json.dumps(message, ensure_ascii=False))
            _watchdog_beat()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error('发送消息失败: %s', exc)
            return


async def run_connection(host, port, token, manifest_data):
    global send_queue

    send_queue = asyncio.Queue()
    uri = 'ws://{}:{}/ws/plugin?token={}'.format(host, port, token)
    try:
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps({
                'op': 'hello',
                'token': token,
                'manifest': manifest_data,
            }, ensure_ascii=False))
            raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            ack = json.loads(raw)
            if not ack.get('accepted'):
                reason = ack.get('reason')
                if reason == 'unknown_plugin_id':
                    logging.error('DGHub 未注册此插件，请先在 插件中心→外部插件 导入 wows_coyote 的 zip')
                    return 'fatal', False
                logging.warning('DGHub 握手被拒绝: %s', reason)
                return 'disconnected', False

            logging.info('✅ 已连接 DGHub，插件启动 (%s)', PLUGIN_ID)
            _watchdog_activate()
            reset_status_cache()
            engine = MixerEngine()
            reader = TelemetryReader(engine)
            update_status('等待遥测文件', step='plugin', state='ok',
                          detail='插件进程已连接 DGHub')
            engine.startup_reset()
            dghub_log('info', '战舰世界 × 郊狼 v2 已启动，等待 ExportShipStatus 遥测')

            tasks = [
                asyncio.create_task(rx_loop(websocket)),
                asyncio.create_task(tx_loop(websocket)),
                asyncio.create_task(telemetry_task(reader)),
                asyncio.create_task(mixer_task(engine)),
            ]
            done = set()
            try:
                done, _ = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                _watchdog_deactivate()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            status = 'disconnected'
            for task in done:
                if task.cancelled():
                    continue
                try:
                    result = task.result()
                except Exception as exc:
                    logging.warning('后台任务异常: %s', exc)
                    continue
                if result == 'stop':
                    status = 'stop'
                    break
            return status, True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.warning('DGHub 连接异常: %s', exc)
        return 'disconnected', False


async def main():
    setup_logging()
    _start_watchdog()
    if _RUNTIME_LOG_PATH:
        logging.info('运行日志文件: %s', _RUNTIME_LOG_PATH)
    if websockets is None:
        logging.error('缺少 websockets 库。DGHub 运行环境自带；手动运行请先 pip install websockets')
        return

    try:
        manifest_data = load_manifest()
    except Exception as exc:
        logging.error('读取 manifest.json 失败: %s', exc)
        return
    if manifest_data.get('id') != PLUGIN_ID:
        logging.warning('manifest.id=%r 与代码 PLUGIN_ID=%r 不一致',
                        manifest_data.get('id'), PLUGIN_ID)

    host = os.environ.get('DGHUB_HOST', '127.0.0.1')
    env_port = os.environ.get('DGHUB_PORT')
    token = os.environ.get('DGHUB_TOKEN')
    port = env_port or '8000'
    delay = 1.0

    while True:
        if not token:
            logging.info('未检测到 DGHUB_TOKEN，开始自动探测 DGHub 端口...')
            ports = []
            if env_port:
                ports.append(env_port)
            if '8000' not in ports:
                ports.append('8000')
            for probe_port in ports:
                logging.info('📡 探测端口 %s ...', probe_port)
                found = await try_connect(host, probe_port, manifest_data)
                if found:
                    token = found
                    port = probe_port
                    break

        if not token:
            logging.error('无法连接 DGHub。请先在 DGHub 中启用本插件，或确认 DGHub 正在运行')
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)
            continue

        status, connected = await run_connection(host, port, token, manifest_data)
        if status == 'stop':
            logging.info('插件已收到停止指令，退出')
            return
        if status == 'fatal':
            return

        if connected:
            delay = 1.0
        latest = get_latest_token(host, port)
        if latest:
            token = latest
        logging.warning('DGHub 连接断开，%.0f 秒后尝试重连...', delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2.0, 30.0)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
